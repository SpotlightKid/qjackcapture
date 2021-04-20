"""Look up "well-known" user directories."""

import os
from os.path import expanduser, join


def get_user_dir(name=None):
    """Look up "well-known" user directories.

    See https://www.freedesktop.org/wiki/Software/xdg-user-dirs/ for more information.

    This reads '$XDG_CONFIG_HOME/user-dirs.dirs' if present. Path values in
    this file may contain environment variables in shell syntax (e.g. "$VAR").
    This function only supports substitution of the environment variable HOME.
    All other environment variables are left unchanged.

    """
    user_dirs_conf = join(os.getenv("XDG_CONFIG_HOME", expanduser("~/.config")), "user-dirs.dirs")
    homedir = os.getenv("HOME", expanduser("~"))
    cfg = {}

    try:
        with open(user_dirs_conf) as fp:
            for line in fp:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue

                var, value = line.split("=", 1)
                value = value.replace("$HOME", homedir)
                value = value.replace("${HOME}", homedir)

                if (value.startswith('"') and value.endswith('"')) or (
                    value.startswith("'") and value.endswith("'")
                ):
                    value = value[1:-1]

                cfg[var] = value
    except (OSError, NameError, TypeError, ValueError):
        pass

    return cfg.get(
        "XDG_{}_DIR".format((name or "desktop").upper()),
        join(homedir, "Desktop") if name == "Desktop" else homedir,
    )


if __name__ == "__main__":
    import sys

    print(get_user_dir(sys.argv[1] if len(sys.argv) > 1 else None))
