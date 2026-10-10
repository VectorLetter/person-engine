"""Container-only configuration adapter; native API CLI defaults stay unchanged."""
import json
import os
from pathlib import Path
import sys

import uvicorn
from .api import create_app
from .api.synthetic_acceptance import SyntheticAcceptanceProvider
from .public_v0 import LocalModels
from .public_v0.store import safe_home


def configure(env):
    home = safe_home(env.get('PERSON_ENGINE_HOME', '/data/person-engine'))
    photo_root = safe_home(env.get('PERSON_ENGINE_PHOTO_ROOT', '/media/photos'))
    model_root = safe_home(env.get('PERSON_ENGINE_MODEL_ROOT', '/models'))
    mode = env.get('PERSON_ENGINE_PROVIDER', 'real')
    if mode not in ('real', 'synthetic_acceptance'):
        raise ValueError('INVALID_PROVIDER_MODE')
    host = env.get('PERSON_ENGINE_HOST', '0.0.0.0')
    if host not in ('0.0.0.0', '127.0.0.1'):
        raise ValueError('INVALID_BIND_ADDRESS')
    port = int(env.get('PERSON_ENGINE_PORT', '8000'))
    if not 1 <= port <= 65535:
        raise ValueError('INVALID_PORT')
    if not photo_root.is_dir() or not model_root.is_dir():
        raise ValueError('MOUNT_DIRECTORY_REQUIRED')
    # Keep caches separate from the dedicated, marker-protected API root.
    for folder in ('tmp', 'cache', 'home'):
        safe_home(home / 'runtime' / folder).mkdir(parents=True, exist_ok=True)
    os.environ.update(TMPDIR=str(home/'runtime/tmp'), TMP=str(home/'runtime/tmp'),
                      TEMP=str(home/'runtime/tmp'), HOME=str(home/'runtime/home'),
                      XDG_CACHE_HOME=str(home/'runtime/cache'))
    provider = None
    if mode == 'synthetic_acceptance':
        provider = SyntheticAcceptanceProvider()
    elif (model_root / 'provider.json').exists():
        config_path = safe_home(model_root / 'provider.json')
        config = json.loads(config_path.read_text())
        for key in ('detector_path', 'embedding_path'):
            path = Path(config[key])
            path = safe_home(path if path.is_absolute() else model_root/path)
            if not path.is_relative_to(model_root):
                raise ValueError('MODEL_PATH_OUTSIDE_MOUNT')
            config[key] = str(path)
        provider = LocalModels(**config)
    return {'root': home/'service', 'provider': provider, 'host': host, 'port': port}


def main():
    try:
        config = configure(os.environ)
    except Exception:
        sys.exit('RUNTIME_CONFIGURATION_ERROR: check mounts, permissions and explicit provider configuration.')
    # Startup errors stay in local logs; HTTP error handling remains the frozen API's.
    uvicorn.run(create_app(config['root'], provider=config['provider']),
                host=config['host'], port=config['port'], workers=1,
                access_log=False, proxy_headers=False)


if __name__ == '__main__':
    main()
