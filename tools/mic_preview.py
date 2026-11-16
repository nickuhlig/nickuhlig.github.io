# Made-in-Canada directory helper (see projects/madeincanada/).
"""Build a local preview of the made-in-Canada directory without Jekyll.

Mimics projects/madeincanada/brands.json (Liquid) in Python and wraps the
directory page in the live site's layout (fetched from www.nuhlig.com).
usage: python -P preview.py <repo> <outdir>
"""
import datetime, glob, json, os, re, shutil, sys, urllib.request
import markdown, yaml

repo, out = sys.argv[1], sys.argv[2]
mic_out = os.path.join(out, 'projects', 'madeincanada')
os.makedirs(os.path.join(mic_out, 'directory'), exist_ok=True)


def split_fm(text):
    m = re.match(r'---\r?\n(.*?)\r?\n---\r?\n?(.*)', text, re.S)
    return yaml.safe_load(m.group(1)) or {}, m.group(2)


vocab = yaml.safe_load(open(os.path.join(repo, '_data', 'madeincanada.yml'), encoding='utf8'))
brands = []
for path in sorted(glob.glob(os.path.join(repo, '_brands', '*.md'))):
    fm, body = split_fm(open(path, encoding='utf8').read())
    lc = fm.get('last_checked')
    brands.append({
        'id': os.path.splitext(os.path.basename(path))[0],
        'title': fm.get('title'),
        'website': fm.get('website'),
        **{k: fm.get(k) for k in ('locations', 'items', 'audience', 'style', 'business',
                                   'made_in_canada', 'status', 'acquired_by', 'acquired_by_website', 'part_of', 'part_of_website', 'ownership')},
        'last_checked': lc.isoformat() if isinstance(lc, datetime.date) else lc,
        'description': markdown.markdown(body).strip(),
    })
json.dump({'vocab': vocab, 'brands': brands}, open(os.path.join(mic_out, 'brands.json'), 'w', encoding='utf8'),
          ensure_ascii=False, indent=1)

# page: live layout around our page body
page_fm, page_body = split_fm(open(os.path.join(repo, 'projects', 'madeincanada', 'directory', 'index.html'), encoding='utf8').read())
live = urllib.request.urlopen('https://www.nuhlig.com/about/').read().decode('utf8')
start = live.index('<div class="content">') + len('<div class="content">')
end = live.index('<footer')
end = live.rindex('</div>', start, end)
html = live[:start] + '\n' + page_body + '\n' + live[end:]
html = re.sub(r'<title>.*?</title>', '<title>' + page_fm['title'] + ' (preview)</title>', html, flags=re.S)
html = re.sub(r'(href|src)="/(?!/)', r'\1="https://www.nuhlig.com/', html)
html = html.replace('url(/', 'url(https://www.nuhlig.com/')
open(os.path.join(mic_out, 'directory', 'index.html'), 'w', encoding='utf8').write(html)
shutil.copy(os.path.join(repo, 'projects', 'madeincanada', 'directory', 'directory.js'), os.path.join(mic_out, 'directory'))
print(len(brands), 'brands ->', mic_out)
