"""Child process only: no initialization, migration or replacement key generation."""
import argparse
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True)
    parser.add_argument("--service", choices=("admin", "parent"), required=True)
    args = parser.parse_args()
    # The controller's environment and argv must identify the same configuration.
    if os.environ.get("AAIE_CRM_RUNTIME_CONFIG") != args.config:
        raise RuntimeError("Runtime configuration argument/environment mismatch")
    from staging_environment import ACTIVE
    if ACTIVE is None or not getattr(ACTIVE, "runtime_configuration", None):
        raise RuntimeError("Managed runtime required")
    ACTIVE.assert_sandbox()
    ACTIVE.runtime_configuration.require_ready()
    os.umask(0o077)
    import app
    port = ACTIVE.admin_port if args.service == "admin" else ACTIVE.parent_port
    app.run_server(ACTIVE.bind_host(args.service), port, args.service, startup_preflight_done=True)


if __name__ == "__main__":
    main()
