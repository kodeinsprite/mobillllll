import pandas as pd, os
path = os.path.join(os.path.dirname(__file__), '..', 'data', 'raw_data.xlsx')
df = pd.read_excel(path, engine='openpyxl', nrows=3)
df.columns = df.columns.str.strip()
print("=== EXCEL SÜTUNLARI ===")
for c in df.columns:
    print(f"  '{c}'")
print()
print("=== İLK 3 SATIR ===")
print(df.head(3).to_string())
