with open('app.py.bak.2026-02-14-085406', 'r') as f:
    lines = f.readlines()

with open('app.py', 'w') as f:
    found_end = False
    for line in lines:
        f.write(line)
        if "</html>" in line.lower() and not found_end:
            # Close the string and add the return
            f.write('\n    """\n    return render_template_string(page)\n')
            found_end = True
            # STOP HERE - do not write the rest of the file (the duplicate mess)
            break

    # Re-append the routes that were likely buried or duplicated below
    # We will grab them from the original backup to be safe
    f.write('\n# --- Restoring missing routes ---\n')
    
# 2. Append the actual logic from the backup while skipping the HTML trash
grep "@app.route" -A 10 /opt/aldiapp/app.py.bak.2026-02-14-085406 | grep -v "<html>" >> app.py

# 3. Add the startup trigger at the very end
cat << 'EOF' >> app.py

if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000)
