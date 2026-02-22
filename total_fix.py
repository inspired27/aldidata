import re

with open('app.py.bak.2026-02-14-085406', 'r') as f:
    content = f.read()

# Fix 1: Wrap the first HTML block properly
if 'page = """' not in content:
    content = content.replace('cfg = _ensure_cfg_shape(cfg)', 'cfg = _ensure_cfg_shape(cfg)\n    page = """')

# Fix 2: Close the first HTML block and add the return statement
# This ensures the home() function ends correctly before other routes start
if 'return render_template_string(page)' not in content:
    content = content.replace('</html>', '</html>\n    """\n    return render_template_string(page)', 1)

# Fix 3: Comment out the SECOND accidental HTML block at the bottom 
# This is what causes the "invalid decimal literal" or "SyntaxError"
content = content.replace('<!doctype html>\n<html>', '"""\n<!doctype html>\n<html>', 1) 
# Note: The above is a trick to turn the second block into a comment/string if it exists

with open('app.py', 'w') as f:
    f.write(content)
