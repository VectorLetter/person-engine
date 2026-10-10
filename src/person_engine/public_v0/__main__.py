"""Local JSON CLI; no HTTP server or private CLI delegation."""
import argparse
import json
from pathlib import Path
from . import (Workspace, initialize, register, ingest, match, LocalModels, create_session,
               fetch_session, save_decisions, apply, person_command, publish_snapshot, read_snapshot, export_snapshot)
from ..core_io import FoundationError


def main(argv=None):
    p = argparse.ArgumentParser(description='Fresh-workspace Public V0; explicit calibration config required')
    p.add_argument('action', choices=['init', 'inspect', 'register', 'ingest', 'match', 'review-create',
                                     'review-get', 'review-save', 'apply', 'publish', 'read-snapshot', 'export', 'person'])
    p.add_argument('--home', required=True)
    p.add_argument('--input', type=Path, help='JSON arguments for selected operation')
    args = p.parse_args(argv)
    try:
        if args.action == 'init':
            result = initialize(args.home).inspect()
        else:
            ws = Workspace(args.home)
            params = json.loads(args.input.read_text()) if args.input else {}
            if args.action == 'inspect':
                result = ws.inspect()
            else:
                functions = {'person': person_command, 'register': register, 'ingest': ingest, 'match': match,
                             'review-create': create_session, 'review-get': fetch_session,
                             'review-save': save_decisions, 'apply': apply, 'publish': publish_snapshot,
                             'read-snapshot': read_snapshot, 'export': export_snapshot}
                if args.action == 'ingest':
                    params['provider'] = LocalModels(**params.pop('local_models'))
                result = functions[args.action](ws, **params)
        print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))
        return 0
    except (FoundationError, ValueError, TypeError, KeyError, OSError) as e:
        print(json.dumps({'error': getattr(e, 'code', 'INVALID_REQUEST'), 'message': str(e)}))
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
