with open("app.py", "r") as f:
    lines = f.readlines()

with open("app.py", "w") as f:
    for i, line in enumerate(lines):
        # We are looking for that broken second assignment at line 848
        # and the raw HTML that follows it.
        if i >= 847: # Line index starts at 0, so 847 is line 848
            if 'page = """' in line or '<!doctype html>' in line.lower():
                continue # Skip this broken line
        f.write(line)
