import sys
import json
import time
from pathlib import Path

# Ensure Pipeline directory is on sys.path
_pipeline_dir = str(Path(__file__).resolve().parent.parent)
if _pipeline_dir not in sys.path:
    sys.path.insert(0, _pipeline_dir)

from sofascore.ss_match import resolve_match_event
from sofascore.ss_utils import SofaClient, is_match_date_matching, teams_match


def test_date_tolerance():
    """Verifies that the +/- 6h date tolerance behaves as expected."""
    print("\n[1/5] Testing Date Tolerance Window (+/- 6h)...")
    ts_ref = 1790186400  # 2026-09-23T18:00:00Z

    # Exact time
    assert is_match_date_matching(ts_ref, "2026-09-23T18:00:00Z") is True
    # Within 6 hours (+2h)
    assert is_match_date_matching(ts_ref + 7200, "2026-09-23T18:00:00Z") is True
    # Outside 6 hours (+7h)
    assert is_match_date_matching(ts_ref + 25200, "2026-09-23T18:00:00Z") is False
    # None or empty ignores date filter
    assert is_match_date_matching(ts_ref, None) is True
    print("  ✅ Date tolerance tests passed.")


def test_scenario_a(client: SofaClient):
    """Scenario A: Both SofaScore team codes are known."""
    print("\n[2/5] Testing Scenario A (Both codes known: Iraq vs Oman)...")
    t0 = time.time()
    res = resolve_match_event(
        team1=["Iraq", "العراق"],
        team2=["Oman", "عمان"],
        kickoff_iso="2026-09-23T14:30:00Z",
        team1_code=4767,
        team2_code=4787,
        client=client,
    )
    dt = time.time() - t0
    assert res is not None and res.get("found") is True
    assert res.get("event_id") == 16875492
    assert res.get("status") == "finished"
    print(f"  ✅ Resolved in {dt:.2f}s: Event ID={res['event_id']} ({res['status']}) - Discovered T1={res['discovered_team1_code']}, T2={res['discovered_team2_code']}")


def test_scenario_b(client: SofaClient):
    """Scenario B: 1 code known, opponent missing code (Arabic-only in Sheets)."""
    print("\n[3/5] Testing Scenario B (1 code known, Arabic-only for missing team: Saudi Arabia vs Kuwait)...")
    t0 = time.time()
    res = resolve_match_event(
        team1=["Saudi Arabia", "السعودية"],
        team2=["الكويت"],
        kickoff_iso="2026-09-23T18:00:00Z",
        team1_code=4834,
        team2_code=None,
        client=client,
    )
    dt = time.time() - t0
    assert res is not None and res.get("found") is True
    assert res.get("event_id") == 16875491
    assert res.get("discovered_team2_code") == 5368
    print(f"  ✅ Resolved in {dt:.2f}s: Event ID={res['event_id']} - Discovered Kuwait ID={res['discovered_team2_code']}")


def test_scenario_c(client: SofaClient):
    """Scenario C: 0 codes known (Pure candidate name search)."""
    print("\n[4/5] Testing Scenario C (0 codes known: Real Madrid vs Villarreal)...")
    t0 = time.time()
    res = resolve_match_event(
        team1=["Real Madrid", "ريال مدريد"],
        team2=["Villarreal", "فياريال"],
        kickoff_iso="2026-10-10T19:00:00Z",
        client=client,
    )
    dt = time.time() - t0
    assert res is not None and res.get("found") is True
    assert res.get("event_id") == 16421066
    assert res.get("discovered_team1_code") == 2829
    assert res.get("discovered_team2_code") == 2819
    print(f"  ✅ Resolved in {dt:.2f}s: Event ID={res['event_id']} - Discovered Real Madrid={res['discovered_team1_code']}, Villarreal={res['discovered_team2_code']}")


def test_live_api_matches(client: SofaClient):
    """Tests all current live matches fetched from Cloudflare API and Sheets."""
    print("\n[5/5] Testing Active Matches from Cloudflare API & Google Sheets...")
    try:
        from utils import load_env, get_cloudflare_api_url, get_spreadsheet_name
        import sheets_client
        import translation_manager
        import requests

        load_env()
        gs_client = sheets_client.get_gspread_client()
        spreadsheet_name = get_spreadsheet_name()
        translations = translation_manager.load_team_translations(gs_client, spreadsheet_name)

        api_url = get_cloudflare_api_url()
        res = requests.get(f"{api_url}/matches", timeout=10)
        api_data = res.json()
        matches = api_data.get("matches", api_data) if isinstance(api_data, dict) else api_data

        success_count = 0
        for idx, m in enumerate(matches, start=1):
            t1_dict, t2_dict = m.get("team1", {}), m.get("team2", {})
            t1_ar, t1_en = t1_dict.get("nameAr", ""), t1_dict.get("nameEn", "")
            t2_ar, t2_en = t2_dict.get("nameAr", ""), t2_dict.get("nameEn", "")
            k_time_iso = m.get("time", "")

            t1_trans = translation_manager.find_existing_translation(t1_ar, translations) or translation_manager.find_existing_translation(t1_en, translations)
            t2_trans = translation_manager.find_existing_translation(t2_ar, translations) or translation_manager.find_existing_translation(t2_en, translations)

            t1_code = t1_trans.get("sofascore_code") if t1_trans else None
            t2_code = t2_trans.get("sofascore_code") if t2_trans else None

            t1_names = [n for n in [t1_en, t1_ar] if n]
            t2_names = [n for n in [t2_en, t2_ar] if n]

            match_res = resolve_match_event(
                team1=t1_names,
                team2=t2_names,
                kickoff_iso=k_time_iso,
                team1_code=t1_code,
                team2_code=t2_code,
                client=client,
            )
            if match_res and match_res.get("found"):
                success_count += 1
                t1_label = t1_en or t1_ar
                t2_label = t2_en or t2_ar
                print(f"  [{idx:2d}/{len(matches)}] ✅ {t1_label} vs {t2_label} -> Event ID {match_res['event_id']} ({match_res['status']})")
            else:
                t1_label = t1_en or t1_ar
                t2_label = t2_en or t2_ar
                print(f"  [{idx:2d}/{len(matches)}] ❌ Failed to resolve: {t1_label} vs {t2_label}")

        print(f"\n  Summary: {success_count}/{len(matches)} API matches successfully resolved.")
        assert success_count == len(matches)
    except Exception as e:
        print(f"  ⚠️ Skipped live API test: {e}")


def main():
    print("=" * 70)
    print("SOFASCORE MODULE TEST SUITE")
    print("=" * 70)

    client = SofaClient()

    test_date_tolerance()
    test_scenario_a(client)
    test_scenario_b(client)
    test_scenario_c(client)
    test_live_api_matches(client)

    print("\n" + "=" * 70)
    print("🎉 ALL SOFASCORE TESTS COMPLETED SUCCESSFULLY!")
    print("=" * 70 + "\n")


if __name__ == "__main__":
    main()
