import sys

with open('app.py.bak.2026-02-14-085406', 'r') as f:
    lines = f.readlines()

with open('app.py', 'w') as f:
    in_html = False
    html_done = False
    
    for i, line in enumerate(lines):
        # 1. Detect the start of the HTML block
        if '<!doctype html>' in line.lower() and not html_done:
            f.write('    page = """\n')
            f.write('    ' + line.lstrip())
            in_html = True
            continue
            
        # 2. While in the HTML block, indent everything by 4 spaces
        if in_html:
            f.write('    ' + line.lstrip())
            # 3. Detect the end of the HTML block
            if '</html>' in line.lower():
                f.write('    """\n')
                f.write('    return render_template_string(page, items=items)\n')
                in_html = False
                html_done = True
            continue

        # 4. Skip the duplicate "trash" HTML block that appears later in the backup
        if '<!doctype html>' in line.lower() and html_done:
            continue
        
        # 5. Prevent double-returns that might have been left over
        if 'return render_template_string' in line and html_done and i < 800:
             continue

        # 6. Write all other Python code (Imports, Routes, etc.)
        f.write(line)

    # 7. Add a clean exit
    f.write('\nif __name__ == "__main__":\n    app.run(host="127.0.0.1", port=5000)\n')
