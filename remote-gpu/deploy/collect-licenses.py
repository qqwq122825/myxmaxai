"""Preserve installed dependency notices in the local test distribution."""
import importlib.metadata
import json
from pathlib import Path
import re
import sys

target = Path(sys.argv[1])
target.mkdir(parents=True, exist_ok=True)
inventory = []
for dist in importlib.metadata.distributions():
    name, version = dist.metadata['Name'], dist.version
    inventory.append({'name': name, 'version': version, 'home': dist.metadata.get('Home-page'),
                      'license': dist.metadata.get('License'), 'project_urls': dist.metadata.get_all('Project-URL') or []})
    for path in dist.files or []:
        if re.search(r'(license|copying|notice)', str(path), re.I):
            source = Path(dist.locate_file(path))
            if source.is_file():
                output = target / name / str(path).replace('..', '_')
                output.parent.mkdir(parents=True, exist_ok=True)
                output.write_bytes(source.read_bytes())
(target / 'inventory.json').write_text(json.dumps(inventory, indent=2, ensure_ascii=False), encoding='utf-8')
