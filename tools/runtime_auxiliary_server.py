"""Native-sandboxed auxiliary worker; no production bootstrap or credentials."""
import argparse
import ctypes
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from runtime_configuration import RuntimeConfiguration
from tools.runtime_auxiliary_control import dependencies, port


def assert_confined(config):
    lib = ctypes.CDLL('/usr/lib/libSystem.B.dylib')
    lib.sandbox_check.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int]
    lib.sandbox_check.restype = ctypes.c_int
    protected = (config.source_root.parent / 'runtime/production/database/staging.db'
                 if config.mode == 'staging' else
                 Path('/Users/faqiang/Downloads/AAIE_CRM_Test_5985902_20261030/STAGING_DATA/database/staging.db'))
    for target in (config.source_root / 'app.py', protected):
        if lib.sandbox_check(os.getpid(), b'file-write-data', 1, str(target).encode()) != 1:
            raise RuntimeError('Auxiliary native sandbox is required')


def run_extraction(config, deps):
    sys.path.insert(0, str(deps['extraction_packages']))
    from content_extraction.repository import ContentExtractionRepository
    from content_extraction_service import build_local_workflow, build_server, build_service_token
    repository = ContentExtractionRepository(config.root / 'database/content_extraction_service.db')
    repository.initialize()
    repository.recover_interrupted_runs()
    token = build_service_token(config.root)
    workflow = build_local_workflow(repository, config.root, environ=os.environ)
    server = build_server('127.0.0.1', config.extraction_port, token, repository, config.root,
                          local_workflow=workflow, source_mode='production' if config.mode == 'production' else 'test',
                          runtime_version=config.mode + '-' + config.config_sha256)
    try:
        server.serve_forever()
    finally:
        server.server_close()


def run_agent(config, deps):
    sys.path[:0] = [str(deps['agent_source']), str(deps['agent_packages'])]
    from xhs_intelligence.web import create_app
    from flask import jsonify
    root = config.root / 'auxiliary/agent-reach'
    app = create_app(dict(DATABASE_PATH=root / 'database.sqlite3',
                          ARTIFACT_ROOT=root / 'artifacts',
                          SEARCH_SMOKE_JOURNAL_PATH=root / 'search-smoke-journal'))

    @app.get('/runtime-health')
    def runtime_health():
        # Use the backend's own DB health check as well as process identity.
        with app.test_client() as client:
            response = client.get('/healthz')
        if response.status_code != 200:
            return jsonify(status='error'), 503
        return jsonify(status='ok', pid=os.getpid(), data_root=str(config.root),
                       config_sha256=config.config_sha256, scheduler_enabled=False,
                       port=config.agent_reach_port)

    # Do not import app.py (its global app uses production defaults); no scheduler.
    app.run(host='127.0.0.1', port=config.agent_reach_port, debug=False, use_reloader=False)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--config', required=True)
    parser.add_argument('--service', choices=('extraction', 'agent-reach'), required=True)
    parser.add_argument('--dependencies-sha256', required=True)
    args = parser.parse_args()
    config = RuntimeConfiguration.load(args.config, source_root=ROOT)
    deps = dependencies(config)
    if deps['sha256'] != args.dependencies_sha256:
        raise RuntimeError('Dependency manifest changed')
    assert_confined(config)
    os.umask(0o077)
    (run_extraction if args.service == 'extraction' else run_agent)(config, deps)


if __name__ == '__main__':
    main()
