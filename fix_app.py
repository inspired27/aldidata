with open('app.py.bak.2026-02-14-085406', 'r') as f:
    lines = f.readlines()

with open('app.py', 'w') as f:
    # Write everything up to the first </html> tag
    # then close the triple quotes and end the function.
    found_first_end = False
    for i, line in enumerate(lines):
        f.write(line)
        if "</html>" in line and not found_first_end:
            f.write('\n    """\n    return render_template_string(page)\n')
            found_first_end = True
            break
