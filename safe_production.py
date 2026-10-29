"""Fail-closed startup contract for the Combined Release safe mode."""

SAFE_PRODUCTION_ENV = "AAIE_SAFE_PRODUCTION"

CLASS_TWO_TASKS = (
    "reminder_engine",
    "academic_results_auto_check",
    "data_update_worker",
    "google_drive_sync",
    "startup_backup",
    "request_backup",
    "interrupted_task_recovery",
    "automatic_data_source_update",
    "school_cache_background_refresh",
    "nas_automatic_scan",
    "student_info_filename_sync",
    "autorestart",
    "snapshot",
    "watchdog",
)


class SafeProductionConfigurationError(RuntimeError):
    pass


def resolve_safe_production(cli_enabled, environ):
    """Return the mode only when the CLI and environment gates agree."""
    cli_enabled = bool(cli_enabled)
    env_value = str(environ.get(SAFE_PRODUCTION_ENV, "")).strip()
    env_enabled = env_value == "1"
    if cli_enabled != env_enabled:
        raise SafeProductionConfigurationError(
            "Safe Production requires both --safe-production and "
            f"{SAFE_PRODUCTION_ENV}=1"
        )
    return cli_enabled and env_enabled
