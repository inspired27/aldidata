with open('app.py.bak.2026-02-14-085406', 'r') as f:
    lines = f.readlines()

with open('app.py', 'w') as f:
    in_home_function = False
    html_captured = False
    
    for line in lines:
        # 1. Detect the start of the home route
        if '@app.route("/")' in line:
            in_home_function = True
            f.write(line)
            continue
            
        # 2. Once inside, find the first HTML tag and start the string
        if in_home_function and '<!doctype html>' in line.lower() and not html_captured:
            f.write('    page = """\n')
            f.write('    ' + line.lstrip()) # Force 4-space indent
            html_captured = True
            continue
        
        # 3. Indent all HTML lines so Python does not crash
        if html_captured and not line.strip().startswith('@app.route'):
            # If it is the end of HTML, close it properly
            if '</html>' in line.lower():
                f.write('    ' + line.lstrip())
                f.write('    """\n')
                f.write('    return render_template_string(page, items=items)\n')
                html_captured = False
                in_home_function = False
            else:
                # Keep indenting the HTML/CSS content
                f.write('    ' + line.lstrip())
            continue

        # 4. Write everything else normally (Routes, etc.)
        # But skip the second "trash" HTML block if it appears
        if '<!doctype html>' in line.lower() and not in_home_function:
            continue
        
        f.write(line)

    # 5. Ensure the runner is at the very end
    f.write('\nif __name__ == "__main__":\n    app.run(host="127.0.0.1", port=5000)\n')
