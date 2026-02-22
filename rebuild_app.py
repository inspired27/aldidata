with open('app.py', 'r') as f:
    lines = f.readlines()

with open('app.py', 'w') as f:
    for line in lines:
        # Stop before the broken line 848 we saw in py_compile
        if 'page = """' in line and lines.index(line) > 800:
            break
        f.write(line)
    
    # Ensure the file ends with the proper Flask runner
    f.write('\nif __name__ == "__main__":\n    app.run(host="127.0.0.1", port=5000)\n')
