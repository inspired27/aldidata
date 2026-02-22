import re

with open('app.py.bak.2026-02-14-085406', 'r') as f:
    content = f.read()

# 1. Find the logic before the HTML and insert the variable start
# We look for the ensure_cfg_shape line and the start of the HTML
pattern_start = r'(cfg = _ensure_cfg_shape\(cfg\)\n)'
replacement_start = r'\1    page = """\n'

if 'page = """' not in content:
    content = re.sub(pattern_start, replacement_start, content)

# 2. Find the FIRST </html> and close the triple quotes + add return
if 'return render_template_string(page)' not in content:
    pattern_end = r'(</html>)'
    replacement_end = r'\1\n    """\n    return render_template_string(page)\n'
    content = re.sub(pattern_end, replacement_end, content, count=1)

with open('app.py', 'w') as f:
    f.write(content)
