# Made-in-Canada directory helper (see projects/madeincanada/).
"""Write _brands/<id>.md files from a section data module.
usage: python -P write_brands.py <section_module.py> <repo>/_brands
The module defines BRANDS = [dict(id=..., title=..., ...), ...]; description is Markdown.
"""
import importlib.util, os, sys

FIELDS = ['website', 'locations', 'items', 'audience', 'style', 'business',
          'made_in_canada', 'status', 'acquired_by', 'acquired_by_website', 'part_of', 'part_of_website', 'ownership', 'last_checked']
LISTS = {'locations', 'items', 'audience', 'style', 'business'}


def q(s):
    return '"' + str(s).replace('\\', '\\\\').replace('"', '\\"') + '"'


def render(b):
    out = ['---', 'title: ' + q(b['title'])]
    for f in FIELDS:
        v = b.get(f)
        if f in LISTS:
            v = v or []
            # quote everything in lists: province codes like ON would otherwise parse as booleans
            out.append(f + ': [' + ', '.join(q(x) for x in v) + ']')
        elif v is None:
            if f in ('acquired_by', 'acquired_by_website', 'part_of', 'part_of_website'):
                continue
            out.append(f + ':')
        else:
            out.append(f + ': ' + (q(v) if f in ('acquired_by', 'part_of') else str(v)))
    out += ['---', b['description'].strip(), '']
    return '\n'.join(out)


spec = importlib.util.spec_from_file_location('section', sys.argv[1])
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
outdir = sys.argv[2]
for b in mod.BRANDS:
    with open(os.path.join(outdir, b['id'] + '.md'), 'w', encoding='utf8', newline='\n') as f:
        f.write(render(b))
    print(f"{b['id']:28} {b['status']:10} {b['made_in_canada']}")
print(len(mod.BRANDS), 'written')
