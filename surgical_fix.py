import os

input_file = 'app.py.bak.2026-02-14-085406'
output_file = 'app.py'

with open(input_file, 'r') as f:
    lines = f.readlines()

new_content = []
in_html_block = False
html_wrapped = False

for i, line in enumerate(lines):
    # Detect the transition from Python to HTML
    if '<!doctype html>' in line.lower() and not html_wrapped:
        # If the line before wasn't the variable assignment, add it
        if i > 0 and 'page = """' not in lines[i-1]:
            new_content.append('    page = """\n')
        in_html_block = True
        new_content.append(line)
        continue
    
    # Detect the end of the first HTML block
    if '</html>' in line.lower() and in_html_block:
        new_content.append(line)
        new_content.append('    """\n')
        new_content.append('    return render_template_string(page)\n')
        in_html_block = False
        html_wrapped = True
        continue

    # If we find the second HTML block start, we comment it out to prevent SyntaxErrors
    if '<!doctype html>' in line.lower() and html_wrapped:
        new_content.append(f"# DUPLICATE HTML REMOVED: {line}")
        continue

    new_content.append(line)

with open(output_file, 'w') as f:
    f.writelines(new_content)
