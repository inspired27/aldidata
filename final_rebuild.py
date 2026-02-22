input_file = 'app.py.bak.2026-02-14-085406'
output_file = 'app.py'

with open(input_file, 'r') as f:
    lines = f.readlines()

with open(output_file, 'w') as f:
    # 1. Write the first 408 lines (The Logic)
    for i in range(408):
        f.write(lines[i])
    
    # 2. Open the HTML string
    f.write('    page = """\n')
    
    # 3. Write the HTML lines until we hit </html>
    html_line_index = 0
    for i in range(408, len(lines)):
        f.write(lines[i])
        if '</html>' in lines[i]:
            html_line_index = i
            break
            
    # 4. Close the string and return with DATA (Fixes Summary)
    f.write('    """\n')
    f.write('    return render_template_string(page, items=items)\n\n')
    
    # 5. Find and write the REST of the routes (Fixes Scheduler 404)
    # We skip any duplicate HTML and start from the next @app.route
    found_rest = False
    for i in range(html_line_index + 1, len(lines)):
        if '@app.route' in lines[i] and not found_rest:
            # Check if this is a "real" route or the duplicate HTML mess
            if 'matrix-all' in lines[i] or 'api/progress' in lines[i]:
                found_rest = True
        
        if found_rest:
            # Stop if we hit that second accidental HTML paste
            if '<!doctype html>' in lines[i].lower():
                break
            f.write(lines[i])

    # 6. Add the runner
    f.write('\nif __name__ == "__main__":\n    app.run(host="127.0.0.1", port=5000)\n')
