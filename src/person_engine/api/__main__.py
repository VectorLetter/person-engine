"""Single-process loopback entry; no network model loading or client provider selection."""
import argparse
import json
from pathlib import Path
import uvicorn
from ..public_v0 import LocalModels
from . import create_app
from .synthetic_acceptance import SyntheticAcceptanceProvider


def main():
    parser = argparse.ArgumentParser(description='Person Engine local-only HTTP adapter')
    parser.add_argument('--root', type=Path)
    parser.add_argument('--port', type=int, default=8000)
    providers = parser.add_mutually_exclusive_group()
    providers.add_argument('--model-config', type=Path, help='Operator-owned LocalModels keyword arguments as JSON')
    providers.add_argument('--synthetic-acceptance', action='store_true',
                           help='TEST/ACCEPTANCE ONLY: deterministic anonymous fixtures; requires explicit --root')
    args = parser.parse_args()
    provider = None
    if args.synthetic_acceptance:
        if args.root is None:
            parser.error('--synthetic-acceptance requires explicit --root for a disposable acceptance workspace')
        provider = SyntheticAcceptanceProvider()
    elif args.model_config:
        try:
            provider = LocalModels(**json.loads(args.model_config.read_text()))
        except Exception:
            parser.exit(2, 'MODEL_UNAVAILABLE: local model configuration could not be loaded.\n')
    uvicorn.run(create_app(args.root if args.root is not None else Path('workspaces'), provider=provider), host='127.0.0.1', port=args.port,
                workers=1, access_log=False, proxy_headers=False)


if __name__ == '__main__':
    main()
