import os
import sys

# Enable ANSI escape characters on Windows
if sys.platform == 'win32':
    try:
        os.system('')
    except OSError as e:
        sys.stderr.write(f"Failed to enable Windows ANSI colors: {e}\n")


# ANSI Escape Sequences for Colors
COLOR_RESET = "\033[0m"
COLOR_BOLD = "\033[1m"

# Foreground Colors (high-intensity)
COLOR_RED = "\033[91m"
COLOR_GREEN = "\033[92m"
COLOR_YELLOW = "\033[93m"
COLOR_BLUE = "\033[94m"
COLOR_CYAN = "\033[96m"
COLOR_DARK_GRAY = "\033[90m"

def step_header(step_num: str, title: str):
    """Prints a styled header for a pipeline step."""
    banner = "=" * 65
    print(f"\n{COLOR_BOLD}{COLOR_CYAN}{banner}{COLOR_RESET}")
    print(f"{COLOR_BOLD}{COLOR_CYAN}>>> [{step_num}] {title.upper()}{COLOR_RESET}")
    print(f"{COLOR_BOLD}{COLOR_CYAN}{banner}{COLOR_RESET}\n")

def info(message: str, indent: int = 2):
    spaces = " " * indent
    print(f"{spaces}{COLOR_BLUE}ℹ{COLOR_RESET}  {message}", flush=True)

def success(message: str, indent: int = 2):
    spaces = " " * indent
    print(f"{spaces}{COLOR_GREEN}✔{COLOR_RESET}  {COLOR_BOLD}{message}{COLOR_RESET}", flush=True)

def warning(message: str, indent: int = 2):
    spaces = " " * indent
    print(f"{spaces}{COLOR_YELLOW}⚠{COLOR_RESET}  {message}", flush=True)

def error(message: str, indent: int = 2):
    spaces = " " * indent
    print(f"{spaces}{COLOR_RED}✘{COLOR_RESET}  {COLOR_RED}{COLOR_BOLD}{message}{COLOR_RESET}", flush=True)

def item(message: str, indent: int = 2):
    spaces = " " * indent
    print(f"{spaces}{COLOR_CYAN}➤{COLOR_RESET}  {message}", flush=True)

def action(action_type: str, message: str, indent: int = 2):
    """Prints a styled reconciliation action line."""
    action_styles = {
        "no_action":         (COLOR_DARK_GRAY, "[ NO ACTION  ]"),
        "assign_new":        (COLOR_GREEN,     "[ ASSIGN NEW ]"),
        "update_sheet_only": (COLOR_YELLOW,    "[ UPD SHEET  ]"),
        "free_slot":         (COLOR_YELLOW,    "[ FREE SLOT  ]"),
        "mark_invalid":      (COLOR_RED,       "[ MARK INVALID]"),
        "restore_slot":      (COLOR_GREEN,     "[ RESTORE SLOT]"),
    }
    color, prefix = action_styles.get(action_type, (COLOR_RESET, f"[{action_type.upper():<14}]"))
    spaces = " " * indent
    if action_type == "no_action":
        print(f"{spaces}{color}{prefix} {message}{COLOR_RESET}")
    else:
        print(f"{spaces}{color}{COLOR_BOLD}{prefix}{COLOR_RESET} {color}{message}{COLOR_RESET}")


def pipeline_end(message: str, is_error: bool = False):
    """Prints a styled end banner for the pipeline."""
    banner = "=" * 65
    color = COLOR_RED if is_error else COLOR_CYAN
    print(f"\n{COLOR_BOLD}{color}{banner}{COLOR_RESET}")
    print(f"{COLOR_BOLD}{color}>>> [END] {message.upper()}{COLOR_RESET}")
    print(f"{COLOR_BOLD}{color}{banner}{COLOR_RESET}\n")



