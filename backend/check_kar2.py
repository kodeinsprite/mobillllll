import pandas as pd
from main import _load_and_process, _apply_category_mappings

metrics, fname = _load_and_process()
metrics = _apply_category_mappings(metrics)

total_ciro = metrics["Satış Tutarı"].fillna(0).sum()
total_smm  = metrics["smm"].fillna(0).sum()
total_kar  = metrics["brut_kar"].fillna(0).sum()

print("=== GENEL TOPLAM ===")
print(f"Satış Tutarı (Ciro): {total_ciro:,.2f}")
print(f"SMM:                 {total_smm:,.2f}")
print(f"Brüt Kar:            {total_kar:,.2f}")
print(f"KAR > CİRO?          {total_kar > total_ciro}")
print()

print("=== KATEGORİ BAZLI ===")
for kat, grp in metrics.groupby("_ana_kategori"):
    ciro = grp["Satış Tutarı"].fillna(0).sum()
    kar  = grp["brut_kar"].fillna(0).sum()
    smm  = grp["smm"].fillna(0).sum()
    flag = " *** HATA: Kar>Ciro ***" if kar > ciro else ""
    print(f"{kat:15s} | Ciro: {ciro:>14,.2f} | SMM: {smm:>14,.2f} | Kar: {kar:>14,.2f}{flag}")

print()
print("=== ÜRÜN BAZINDA KAR>CİRO OLAN SATIRLAR ===")
bad = metrics[metrics["brut_kar"].fillna(0) > metrics["Satış Tutarı"].fillna(0)]
print(f"Toplam {len(bad)} adet sorunlu satır")
if len(bad) > 0:
    print(bad[["Stok Kodu","Satış Miktarı","birim_alis_fiyati","smm","Satış Tutarı","brut_kar","psf"]].head(20).to_string())

print()
print("=== NEGATİF SMM KONTROL ===")
neg = metrics[metrics["smm"].fillna(0) < 0]
print(f"Negatif SMM satır sayısı: {len(neg)}")
if len(neg) > 0:
    print(neg[["Stok Kodu","Satış Miktarı","birim_alis_fiyati","smm"]].head(10).to_string())

print()
print("=== ANALYTICS ENDPOINT SİMÜLASYONU ===")
# Analytics endpoint'in total_ciro ve total_kar nasıl hesapladığını simüle et
print(f"total_satis: {metrics['Satış Miktarı'].fillna(0).sum():.0f}")
print(f"total_ciro (Satış Tutarı): {metrics['Satış Tutarı'].fillna(0).sum():,.2f}")
print(f"total_kar (brut_kar): {metrics['brut_kar'].fillna(0).sum():,.2f}")

# Toplam Kar sütunu var mı?
if "Toplam Kar" in metrics.columns:
    print(f"Toplam Kar sütunu: {metrics['Toplam Kar'].fillna(0).sum():,.2f}")
if "toplam_kar" in metrics.columns:
    print(f"toplam_kar sütunu: {metrics['toplam_kar'].fillna(0).sum():,.2f}")
