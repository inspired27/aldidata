with open('app.py.bak.2026-02-14-085406', 'r') as f:
    lines = f.readlines()

with open('app.py', 'w') as f:
    html_found = 0
    for line in lines:
        # Check for the start of an HTML block
        if '<!doctype html>' in line.lower():
            html_found += 1
        
        # If this is the SECOND time we see HTML, stop writing the file.
        # This removes the duplicate CSS/HTML that is crashing the app.
        if html_found > 1:
            break
        f.write(line)
    
    # Append the necessary startup logic that was likely lost in the duplicate paste
    f.write('\nif __name__ == "__main__":\n    app.run(host="0.0.0.0", port=5000)\n')
