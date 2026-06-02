import pandas as pd
from main import _load_and_process

metrics, fname = _load_and_process()
total_ciro = metrics["Satış Tutarı"].fillna(0).sum()
total_smm = metrics["smm"].fillna(0).sum()
total_kar = metrics["brut_kar"].fillna(0).sum()

print(f"Toplam Ciro (Satış Tutarı sum): {total_ciro:,.2f}")
print(f"Toplam SMM (qty * alis sum): {total_smm:,.2f}")
print(f"Toplam Kar (brut_kar sum): {total_kar:,.2f}")

neg_smm = metrics[metrics["smm"] < 0]
print(f"Negatif SMM olan satır sayısı: {len(neg_smm)}")
if len(neg_smm) > 0:
    print("Örnek negatif SMM satırları:")
    print(neg_smm[["Stok Kodu", "Satış Miktarı", "birim_alis_fiyati", "smm", "Satış Tutarı", "brut_kar"]].head())

