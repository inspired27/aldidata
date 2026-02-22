with open('app.py.bak.2026-02-14-085406', 'r') as f:
    lines = f.readlines()

with open('app.py', 'w') as f:
    for i, line in enumerate(lines):
        # We find the specific logic line we saw in sed earlier
        if 'cfg = _ensure_cfg_shape(cfg)' in line:
            f.write(line)
            # Check if the next line is already the start of the string
            # If not, we insert the missing assignment
            if i + 1 < len(lines) and 'page = """' not in lines[i+1]:
                f.write('    page = """\n')
        else:
            f.write(line)
