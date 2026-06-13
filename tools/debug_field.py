import sys, json
sys.path.insert(0, '.')
from pathlib import Path
from worlds.ff7.biton_mapper import parse_gs_debug

debug_path = Path(r'D:\SteamLibrary\steamapps\common\FINAL FANTASY VII_randomized\field_randomization_debug.txt')
_, placements = parse_gs_debug(debug_path)

print(f'Total placements: {len(placements)}')
print()
print('=== UNRESOLVED (bank=-1) ===')
for p in placements:
    if p['bank'] == -1:
        print(f"  '{p['item_name']}' src={p['src_field']}@{p['src_offset']} -> dest={p['dest_field']}@{p['dest_offset']}")

print()
print('=== RESOLVED ===')
for p in placements:
    if p['bank'] != -1:
        print(f"  '{p['item_name']}' -> bank={p['bank']} addr={p['address']} bit={p['bit']}  dest={p['dest_field']}@{p['dest_offset']}")
