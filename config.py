import os
from getpass import getpass
from pathlib import Path


DEFAULT_ROOT_DIR = Path.home() / "Downloads" / "Blackboard"


def _get_saved_value(name):
    env_value = os.getenv(f"BLACKBOARD_{name}")
    if env_value:
        return env_value

    try:
        import key
    except ModuleNotFoundError as exc:
        if exc.name != "key":
            raise
        return None

    return getattr(key, name, None)


def _prompt_value(name, prompt, secret=False, default=None):
    saved_value = _get_saved_value(name)
    if saved_value:
        return saved_value

    if default:
        entered = input(f"{prompt} [{default}]: ").strip()
        return entered or str(default)

    if secret:
        return getpass(f"{prompt}: ")

    return input(f"{prompt}: ").strip()


def get_email():
    return _prompt_value("EMAIL", "Imperial email")


def get_password():
    return _prompt_value("PASSWORD", "Imperial password", secret=True)


def get_root_dir():
    return _prompt_value("ROOT_DIR", "Download folder", default=DEFAULT_ROOT_DIR)
