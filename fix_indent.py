with open('kalshieye_downloader.py', 'r') as f:
    lines = f.readlines()

# Fix lines 219-236 (0-indexed: 218-235): dedent from 16 spaces to 12 spaces
for i in range(218, 236):
    lines[i] = lines[i].replace('                ', '            ', 1)

with open('kalshieye_downloader.py', 'w') as f:
    f.writelines(lines)

print('Fixed indentation')
