from main import _load_and_process, _apply_category_mappings
metrics, fname = _load_and_process()
metrics = _apply_category_mappings(metrics)
print("=== SÜTUNLAR ===")
for c in sorted(metrics.columns):
    print(f"  '{c}'")
print()
print("=== 'RENK' İÇEREN SÜTUNLAR ===")
for c in metrics.columns:
    if 'renk' in c.lower() or 'RENK' in c:
        print(f"  '{c}' -> unique count: {metrics[c].nunique()}")
print()
print("=== 'CİNSİYET' İÇEREN SÜTUNLAR ===")
for c in metrics.columns:
    if 'cinsiyet' in c.lower() or 'CİNSİYET' in c:
        print(f"  '{c}' -> unique: {metrics[c].dropna().unique().tolist()[:10]}")
