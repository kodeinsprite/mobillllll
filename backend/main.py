"""
Bestseller / Worstseller API — iş kuralları özeti:

1) Gruplama (ham satırlar): Stok Kodu + tanımlayıcılar.
   - Toplam Satış Miktarı, Ciro (Satış Tutarı), DSS toplamları: sum
   - Birim Alış Fiyatı ve PSF: satır bazında hazırlanıp groupby sonrası mean (prompt ile uyumlu)

2) KPI (gruplanmış satır başına, vektörel):
   - Toplam SMM = birim_alis * Toplam Satış Miktarı
   - Initial Ciro (liste/potansiyel ciro) = PSF * Toplam Satış Miktarı
   - İndirim Oranı = 1 − (gerçek Ciro / Initial Ciro)
   - Toplam Kar = Ciro − Toplam SMM
   - MU = Ciro / Toplam SMM
   - Sell Through % = Satış / (Satış + DSS) * 100

3) Sıralama: tam liste Satış Miktarı DESC; Bestseller = ilk 10.
   Worstseller: DSS > 0, Satış Miktarı ASC, ilk 10.
"""

from pathlib import Path
import asyncio
import json
import math
import re
import os
import time
import urllib.request
import urllib.error
import urllib.parse
from concurrent.futures import ThreadPoolExecutor
import pandas as pd
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse, Response
from fastapi.staticfiles import StaticFiles

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
CSV_PATH = DATA_DIR / "raw_data.csv"
XLSX_PATH = DATA_DIR / "raw_data.xlsx"

# Static assets (for cached images)
STATIC_ROOT = Path(__file__).resolve().parent / "static"
IMAGES_DIR = STATIC_ROOT / "images"

VAT_DEFAULT = 0.0  # Raporlardaki Brüt Kar (Sales - Cost) ile eşleşmesi için KDV %0 set edildi

GROUP_COLS = [
    "Stok Kodu",
    "Stok Kodu Açıklama",
    "ANAGRUP",
    "Alt Kategori",
    "CİNSİYET",
]

SUMMARY_MARKERS = frozenset(
    {"Toplam Satış Miktarı", "Toplam DSS Miktar", "Ciro"},
)

# Özet Excel: KPI’lar dosyadan değil kuralla yeniden hesaplanır (Toplam Kar dahil).
SUMMARY_REQUIRED = GROUP_COLS + [
    "Toplam Satış Miktarı",
    "Toplam DSS Miktar",
    "Ciro",
    "Alış Fiyat",
    "PSF",
]

EXPECTED_COLUMNS = GROUP_COLS + [
    "Marka Açıklama",
    "Mevcut Sezon Kodu",
    "E-TİCARET RENK",
    "Renk Açıklama",
    "E-TİCARET CİNSİYET",
    "E-TİCARET MEVSİM",
    "ALT/ÜST",
    "Alış Miktarı",
    "Alış Tutarı",
    "Satış Miktarı",
    "Satış Tutarı",
    "Satış Oranı",
    "DSS Miktar",
    "Son Stok Miktarı",
    "PB.",
    "DBS Miktar",
    "Ortalama Stok",
]

app = FastAPI(title="Bestseller Dashboard API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Ensure static directories exist and mount at /static
STATIC_ROOT.mkdir(parents=True, exist_ok=True)
IMAGES_DIR.mkdir(parents=True, exist_ok=True)
app.mount("/static", StaticFiles(directory=STATIC_ROOT), name="static")


_prefetch_pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="img_prefetch")


async def _background_prefetch_images(skus: list[str]) -> None:
    """Pre-resolve images for every SKU in the background without blocking the server."""
    loop = asyncio.get_event_loop()
    for sku in skus:
        if not sku or sku in _img_resolve_cache:
            continue
        try:
            await loop.run_in_executor(_prefetch_pool, _resolve_any_image, sku)
        except Exception:
            pass
        await asyncio.sleep(0.25)  # rate-limit DDG


@app.on_event("startup")
async def _startup_populate_meta_cache():
    """Pre-populate caches and kick off background image prefetch."""
    global _sku_raw_url_cache
    try:
        raw_links = _load_raw_image_links()
        _sku_raw_url_cache.update(raw_links)
    except Exception:
        pass
    try:
        metrics, _ = _load_and_process()
        metrics = _apply_category_mappings(metrics)
        skus = (
            metrics["Stok Kodu"]
            .dropna()
            .astype(str)
            .str.strip()
            .unique()
            .tolist()
        )
        asyncio.create_task(_background_prefetch_images(skus))
    except Exception:
        pass


def _normalize_columns(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df.columns = df.columns.str.strip()
    return df


def _txt_clean(x) -> str:
    if pd.isna(x):
        return ""
    s = str(x).strip()
    return "" if s.lower() == "nan" else s


def _to_numeric_series(s: pd.Series) -> pd.Series:
    # Zaten sayısal sütunları tekrar metin olarak ayrıştırma
    # (3 ondalık basamaklı float'lar binlik ayraç sanılıp 1000x şişiriliyordu)
    if pd.api.types.is_numeric_dtype(s):
        return s
    text = s.astype(str).str.strip()
    text = text.str.replace(r"\s*(adet|pcs|ad\.|piece|units?)\s*", "", regex=True, flags=re.IGNORECASE)
    text = text.str.replace(r"[^\d,.\-]", "", regex=True)
    text = text.str.replace(r"\.(?=\d{3}(?:[,.]|$))", "", regex=True)
    text = text.str.replace(",", ".", regex=False)
    text = text.replace({"": pd.NA, "-": pd.NA, ".": pd.NA})
    return pd.to_numeric(text, errors="coerce")


def _clean_numeric_values(df: pd.DataFrame, columns: list) -> pd.DataFrame:
    """
    Metinsel sayıları temizleyerek matematiksel formata çevirir.
    Örn: "15 adet" → 15, "0,0" → 0.0, "1.234,56" → 1234.56
    """
    df = df.copy()
    for col in columns:
        if col in df.columns:
            df[col] = _to_numeric_series(df[col])
    return df


def _standardize_brand_names(df: pd.DataFrame, column: str = "Marka Açıklama") -> pd.DataFrame:
    """
    Marka isimlerini standardize eder.
    Örn: "adidas", "ADIDAS", " Adidas " → "Adidas"
    """
    df = df.copy()
    if column in df.columns:
        # Önce temizle
        df[column] = df[column].astype(str).str.strip()
        # Küçük harfe çevir ve baş harfleri büyük yap
        df[column] = df[column].str.title()
        # Bilinen markalar için düzeltmeler
        brand_mappings = {
            "Adıdas": "Adidas",
            "Adidas Originals": "Adidas",
            "Nıke": "Nike",
            "Puma Se": "Puma",
            "The North Face": "The North Face",
        }
        for old, new in brand_mappings.items():
            df[column] = df[column].replace(old, new)
    return df


def _remove_duplicates(df: pd.DataFrame) -> pd.DataFrame:
    """
    Birbiriyle tamamen aynı olan yinelenen satırları ayıklar.
    """
    before = len(df)
    df = df.drop_duplicates(keep='first')
    after = len(df)
    if before != after:
        print(f"[VERİ TEMİZLİK] {before - after} yinelenen satır kaldırıldı.")
    return df


def _handle_division_by_zero(df: pd.DataFrame) -> pd.DataFrame:
    """
    Sıfıra bölme hatalarını önler. Satış ve DSS 0 olanları %0 olarak işaretler.
    """
    df = df.copy()
    # Sell Through hesaplaması için kontrol
    if 'Satış Miktarı' in df.columns and 'DSS Miktar' in df.columns:
        total_stock = df['Satış Miktarı'].fillna(0) + df['DSS Miktar'].fillna(0)
        # Toplam stok 0 ise Sell Through'u 0 olarak işaretle (NaN değil)
        df['_zero_stock_flag'] = (total_stock == 0)
    return df


def _is_summary_export(df: pd.DataFrame) -> bool:
    return SUMMARY_MARKERS.issubset(df.columns)


def _apply_kpi_rules(out: pd.DataFrame, vat_rate: float | None = VAT_DEFAULT) -> pd.DataFrame:
    """
    Gruplanmış/özet satırlar üzerinde iş mantığı KPI’ları.
    Kolonlar: Satış Miktarı, Satış Tutarı (ciro), DSS Miktar, birim_alis_fiyati, psf.
    """
    out = out.copy()
    qty = out["Satış Miktarı"].fillna(0).astype(float)
    ciro_net = out["Satış Tutarı"].fillna(0).astype(float)  # iskontolu (net) ciro
    dss = out["DSS Miktar"].fillna(0).astype(float)
    alis = _to_numeric_series(out["birim_alis_fiyati"]).astype(float)
    psf = _to_numeric_series(out["psf"])  # NA tutulacak — astype(float) yapma

    # ── Brüt Ciro: PSF varsa Price×Qty (iskontosuz), yoksa Satış Tutarı ──
    brut_ciro = (qty * psf).where(psf.notna(), other=ciro_net)

    # Toplam SMM = sabit birim maliyet × satış adedi
    toplam_smm = qty * alis
    out["smm"] = toplam_smm

    # Initial Ciro = liste PSF × satış adedi — PSF yoksa NA
    initial_ciro = qty * psf  # psf NA ise initial_ciro de NA olur
    out["initial_ciro"] = initial_ciro.where(psf.notna(), other=pd.NA)

    # İndirim Oranı = 1 − (Net Ciro / Brüt Ciro) — PSF yoksa NA
    out["indirim_orani"] = (1 - ciro_net.div(brut_ciro.replace(0, pd.NA))).where(psf.notna(), other=pd.NA)

    # UYUM İÇİN DÜZELTME: Kar hesaplamaları Net Ciro (Satış Tutarı) üzerinden yapılmalıdır.
    out["brut_kar"] = ciro_net - toplam_smm.fillna(0)
    out["toplam_kar"] = ciro_net - toplam_smm.fillna(0)

    # MU (mark-up çarpanı) = Brüt Ciro / Toplam SMM (Retail'de MU genellikle iskontosuz liste fiyatı üzerinden alınır)
    out["mu"] = brut_ciro.div(toplam_smm.replace(0, pd.NA))

    # Sell Through % = Satış / (Satış + DSS) × 100
    out["sell_through_pct"] = qty.div((qty + dss).replace(0, pd.NA)) * 100
    out["periyot_cover_19"] = dss.div(qty.replace(0, pd.NA)) * 19
    # 0 satışlı ürünlerde Cover = 1000 (referans Excel standardı)
    out.loc[qty == 0, "periyot_cover_19"] = 1000

    # Kar Marjı % = (Brüt Kar / Net Ciro) × 100
    out["kar_marji_pct"] = out["brut_kar"].div(ciro_net.replace(0, pd.NA)) * 100

    # GMROI (opsiyonel klasik tanım): Ortalama Stok × birim alışa göre yıllıklaştırma
    if "Ortalama Stok" in out.columns:
        ort = pd.to_numeric(out["Ortalama Stok"], errors="coerce").fillna(0).astype(float)
        denom = ort * alis
        out["gmroi"] = out["brut_kar"].div(denom.replace(0, pd.NA)) * 52
    else:
        out["gmroi"] = pd.NA

    return out


def _process_summary_export(df: pd.DataFrame) -> pd.DataFrame:
    missing = [c for c in SUMMARY_REQUIRED if c not in df.columns]
    if missing:
        raise HTTPException(
            status_code=400,
            detail=f"Özet Excel'de eksik sütunlar: {missing}",
        )

    base = pd.DataFrame()
    for col in GROUP_COLS:
        base[col] = df[col].map(_txt_clean)

    if "Marka Açıklama" in df.columns:
        base["Marka Açıklama"] = df["Marka Açıklama"].map(_txt_clean)
    else:
        base["Marka Açıklama"] = ""

    base["Satış Miktarı"] = _to_numeric_series(df["Toplam Satış Miktarı"]).fillna(0)
    base["DSS Miktar"] = _to_numeric_series(df["Toplam DSS Miktar"]).fillna(0)
    base["Satış Tutarı"] = _to_numeric_series(df["Ciro"]).fillna(0)

    base["birim_alis_fiyati"] = _to_numeric_series(df["Alış Fiyat"]).astype(float)
    base["psf"] = _to_numeric_series(df["PSF"]).astype(float)

    # Bestseller/Worstseller endpoint'lerinin ihtiyaç duyduğu ek sütunlar
    for opt_col in ["E-TİCARET RENK", "Renk Açıklama", "E-TİCARET CİNSİYET", "E-TİCARET MEVSİM"]:
        if opt_col in df.columns:
            base[opt_col] = df[opt_col].map(_txt_clean)
        else:
            base[opt_col] = ""

    if "Mevcut Sezon Kodu" in df.columns:
        base["Mevcut Sezon Kodu"] = df["Mevcut Sezon Kodu"].map(_txt_clean)
    elif "İlk Sezon Kodu" in df.columns:
        base["Mevcut Sezon Kodu"] = df["İlk Sezon Kodu"].map(_txt_clean)
    else:
        base["Mevcut Sezon Kodu"] = ""

    base["Ortalama Stok"] = pd.NA
    if "Periyot Cover (19 hafta)" in df.columns:
        base["_cover_legacy"] = _to_numeric_series(df["Periyot Cover (19 hafta)"]).astype(float)
    else:
        base["_cover_legacy"] = pd.NA

    if "Görsel Link" in df.columns:
        base["gorsel_link"] = df["Görsel Link"].map(_txt_clean)
    elif "Resim" in df.columns:
        base["gorsel_link"] = df["Resim"].map(_txt_clean)
    else:
        base["gorsel_link"] = ""

    base = _apply_kpi_rules(base, VAT_DEFAULT)

    # Diagnostik alanlar (ham veride mevcut olmadığı için NA):
    base["alis_qty_sum"] = pd.NA
    base["alis_total_sum"] = pd.NA

    # Özet dosyada GMROI sütunu varsa raporda gösterim için sakla (iş kuralı zorunlu değil)
    if "_cover_legacy" in base.columns and base["_cover_legacy"].notna().any():
        base["gmroi"] = base["_cover_legacy"]
    base.drop(columns=["_cover_legacy"], inplace=True, errors="ignore")

    return base


def _aggregate_transactional(df: pd.DataFrame) -> pd.DataFrame:
    """Ham çoklu satırları grup bazında tekilleştirir; birim alış için ağırlıklı ortalama, PSF için mean."""
    df = df.copy()

    # ── Birim Maliyet: Standard Cost > Alış Fiyatı > Alış Tutarı/Alış Miktarı ──
    if "Birim Maliyet" in df.columns:
        df["_alis_unit"] = _to_numeric_series(df["Birim Maliyet"])
    elif "Standard Cost" in df.columns:
        df["_alis_unit"] = _to_numeric_series(df["Standard Cost"])
    elif "Unit Cost" in df.columns:
        df["_alis_unit"] = _to_numeric_series(df["Unit Cost"])
    elif "Alış Fiyatı" in df.columns:
        df["_alis_unit"] = _to_numeric_series(df["Alış Fiyatı"])
    else:
        am = df["Alış Miktarı"].replace(0, pd.NA)
        df["_alis_unit"] = df["Alış Tutarı"].div(am)

    if "Alış Miktarı" in df.columns:
        df["_alis_qty"] = _to_numeric_series(df["Alış Miktarı"])
    else:
        df["_alis_qty"] = pd.NA

    if "Alış Tutarı" in df.columns:
        df["_alis_total"] = _to_numeric_series(df["Alış Tutarı"])
    else:
        df["_alis_total"] = pd.NA

    # ── PSF (Piyasa Satış Fiyatı): Sütun varsa kullan, yoksa hesapla ──
    if "PSF" in df.columns:
        df["_psf_unit"] = _to_numeric_series(df["PSF"])
    else:
        # PSF = Satış Tutarı / Satış Miktarı (birim etiket fiyatı)
        satis_m = _to_numeric_series(df["Satış Miktarı"]).replace(0, pd.NA)
        satis_t = _to_numeric_series(df["Satış Tutarı"])
        df["_psf_unit"] = satis_t.div(satis_m)  # Satış Miktarı=0 → NaN

    agg_spec: dict = {
        "Marka Açıklama": "first",
        "Mevcut Sezon Kodu": "first",
        "E-TİCARET RENK": "first",
        "Renk Açıklama": "first",
        "E-TİCARET CİNSİYET": "first",
        "E-TİCARET MEVSİM": "first",
        "Satış Miktarı": "sum",
        "Satış Tutarı": "sum",
        "DSS Miktar": "sum",
        "Ortalama Stok": "mean",
        "_alis_unit": "mean",   # fallback için tutulur
        "_alis_qty": "sum",
        "_alis_total": "sum",
        "_psf_unit": "mean",
    }
    g = df.groupby(GROUP_COLS, dropna=False, as_index=False).agg(agg_spec)

    # Birim alış fiyatı: DOĞRU FORMÜL = sum(Alış Tutarı) / sum(Alış Miktarı)
    # (Basit mean kullanmak, farklı miktarlardaki satırları eşit ağırlıklı sayar → yanlış SMM)
    # Standard Cost/Birim Maliyet sütunu varsa _alis_unit sabit maliyet olarak kullanılır.
    has_standard_cost = any(c in df.columns for c in ("Birim Maliyet", "Standard Cost", "Unit Cost", "Alış Fiyatı"))
    if has_standard_cost:
        g["birim_alis_fiyati"] = g["_alis_unit"]
    else:
        # Ağırlıklı ortalama: toplam maliyet / toplam alış adedi
        g["birim_alis_fiyati"] = g["_alis_total"].div(g["_alis_qty"].replace(0, pd.NA))
        g["birim_alis_fiyati"] = g["birim_alis_fiyati"].fillna(g["_alis_unit"])

    # Diagnostik toplamlar (endpoint'lerde inceleme için tutulur)
    g["alis_qty_sum"] = g["_alis_qty"]
    g["alis_total_sum"] = g["_alis_total"]

    g.rename(columns={"_psf_unit": "psf"}, inplace=True)
    g.drop(columns=["_alis_unit", "_alis_qty", "_alis_total"], inplace=True, errors="ignore")
    return g


def _row_to_product(row: pd.Series) -> dict:
    def num(v, ndigits=None):
        # pandas.NA / numpy.nan — float() güvenli değil
        if v is None:
            return None
        try:
            if pd.isna(v):
                return None
        except (TypeError, ValueError):
            pass
        try:
            fv = float(v)
        except (TypeError, ValueError):
            return None
        if math.isnan(fv) or math.isinf(fv):
            return None
        if ndigits is not None:
            return round(fv, ndigits)
        return fv

    gl = row.get("gorsel_link")
    try:
        if gl is not None and pd.isna(gl):
            gl = None
    except TypeError:
        pass
    if gl == "":
        gl = None
    elif gl is not None:
        gl = str(gl).strip() or None

    tk_toplam = num(row.get("toplam_kar"), 2)
    tk_brut = num(row.get("brut_kar"), 2)
    tk = tk_toplam if tk_toplam is not None else tk_brut

    return {
        "stok_kodu": row.get("Stok Kodu"),
        "stok_aciklama": row.get("Stok Kodu Açıklama"),
        "marka_aciklama": row.get("Marka Açıklama"),
        "e_ticaret_renk": row.get("E-TİCARET RENK"),
        "renk_aciklama": row.get("Renk Açıklama"),
        "e_ticaret_cinsiyet": row.get("E-TİCARET CİNSİYET"),
        "e_ticaret_mevsim": row.get("E-TİCARET MEVSİM"),
        "mevcut_sezon_kodu": row.get("Mevcut Sezon Kodu"),
        "anagrup": row.get("ANAGRUP"),
        "alt_kategori": row.get("Alt Kategori"),
        "cinsiyet": row.get("CİNSİYET"),
        "satis_miktari": num(row.get("Satış Miktarı")),
        "satis_tutari": num(row.get("Satış Tutarı"), 2),
        "dss_miktari": num(row.get("DSS Miktar")),
        "ortalama_stok": num(row.get("Ortalama Stok"), 2),
        "birim_alis_fiyati": num(row.get("birim_alis_fiyati"), 4),
        "psf": num(row.get("psf"), 2),
        "initial_ciro": num(row.get("initial_ciro"), 2),
        "indirim_orani": num(row.get("indirim_orani"), 6),
        "smm": num(row.get("smm"), 2),
        "toplam_kar": tk_toplam if tk_toplam is not None else None,
        "brut_kar": tk_brut,
        "mu": num(row.get("mu"), 4),
        "sell_through_pct": num(row.get("sell_through_pct"), 2),
        "periyot_cover_19": num(row.get("periyot_cover_19"), 2),
        "gmroi": num(row.get("gmroi"), 4),
        "kar_marji_pct": num(row.get("kar_marji_pct"), 2),
        "gorsel_link": f"/api/image/{str(row.get('Stok Kodu') or '').strip()}",
        "product_url": _product_page_for_sku(str(row.get("Stok Kodu") or "")),
    }


def _worstseller_pool(df: pd.DataFrame) -> pd.DataFrame:
    """Worstseller adayları: Stokta olan (DSS > 0) ve E-TİCARET RENK bilgisi dolu olanlar."""
    # E-TİCARET RENK boş olmamalı ve "999 - BOŞ" olmamalı
    pool = df[df["DSS Miktar"] > 0].copy()
    if "E-TİCARET RENK" in pool.columns:
        pool["E-TİCARET RENK"] = pool["E-TİCARET RENK"].fillna("").astype(str).str.strip()
        pool = pool[
            (pool["E-TİCARET RENK"] != "") & 
            (pool["E-TİCARET RENK"] != "999 - BOŞ")
        ]
    return pool


def _dead_stock_pool(df: pd.DataFrame) -> pd.DataFrame:
    """Ölü Stok adayları: Satışı olmayan (Satış == 0) ama DSS > 0 olanlar."""
    pool = df[(df["Satış Miktarı"] == 0) & (df["DSS Miktar"] > 0)].copy()
    return pool


def _sort_worstsellers(pool: pd.DataFrame) -> pd.DataFrame:
    """
    Sıralama: Sell Through % ASC → DSS Miktar DESC
    En düşük satış oranına (ST%) sahip olanlar (özellikle %0 olan 0-satışlılar) en üstte yer alır.
    Aynı ST% içinde (örneğin hepsi %0 ise), DSS miktarı en yüksek olan (en büyük stok riski) önce gelir.
    """
    return pool.sort_values(
        ["sell_through_pct", "DSS Miktar"],
        ascending=[True, False],
        na_position="last",
        kind="mergesort",
    )


def resolve_data_path() -> Path:
    if CSV_PATH.is_file():
        return CSV_PATH
    if XLSX_PATH.is_file():
        return XLSX_PATH
    raise HTTPException(
        status_code=404,
        detail=(
            f"Veri dosyası bulunamadı. Şunlardan birini ekleyin: "
            f"{CSV_PATH.name} veya {XLSX_PATH.name} → {DATA_DIR}"
        ),
    )


_cache = {}

# SKU -> image URL cache (to avoid repeated network probes)
_image_cache: dict[str, str | None] = {}

# In-memory resolved image URL cache (no disk writes)
_img_resolve_cache: dict[str, str | None] = {}

# Raw gorsel_link values from data file ("Görsel Link" / "Resim" column)
_sku_raw_url_cache: dict[str, str] = {}

# SKU -> internal Sporthink product ID
_GORSEL_ID_MAP: dict[str, str] = {
    "JM6535": "162492", "JN2709": "162509", "ID3807": "158727",
    "JN2708": "162508", "VN000H4WEMJ1": "163021", "NF0A3VXF0IT1": "154848",
    "VN000H4W12B1": "163012", "VN000H4W6671": "163015", "VN000H4W6701": "163016",
    "JI3470": "158369", "JI1545": "158382", "IF4136": "158682",
    "IH2844": "152569", "JI0910": "158388", "ID2855": "148433",
    "JI1710": "158379", "JI1541": "158381", "JH8620": "158378",
    "JW2435": "158390", "JE1188": "158376", "JY3484": "162685",
    "JX3914": "162681", "IK4009": "152778", "JI8960": "161295",
    "JN9930": "160759", "JM3113": "160354",
}

# SKU -> (brand, description) for web search fallback
_sku_meta_cache: dict[str, tuple[str, str]] = {}

def _http_exists(url: str, timeout: float = 0.6) -> bool:
    try:
        req = urllib.request.Request(url, method="HEAD", headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            code = getattr(resp, "status", None) or resp.getcode()
            return 200 <= int(code) < 400
    except Exception:
        return False

def _cdn_candidates_for_sku(sku: str, size: int = 300) -> list[str]:
    k = (sku or "").strip()
    if not k:
        return []
    ku = k.upper()
    s = int(size)
    kl = k.lower()
    idx = ["", "_1", "_2", "_3", "_4", "_5"]
    patterns = []
    for sku_variant in (ku, kl):
        for suffix in idx:
            # underscore form
            patterns.extend([
                f"https://sporthink.sm.mncdn.com/mnresize/{s}/{s}/sporthink/uploads/products/original_{sku_variant}{suffix}.jpeg",
                f"https://sporthink.sm.mncdn.com/mnresize/{s}/{s}/sporthink/uploads/products/original_{sku_variant}{suffix}.jpg",
                f"https://sporthink.sm.mncdn.com/sporthink/uploads/products/original_{sku_variant}{suffix}.jpeg",
                f"https://sporthink.sm.mncdn.com/sporthink/uploads/products/original_{sku_variant}{suffix}.jpg",
            ])
            # hyphen form sometimes used
            if suffix.startswith("_"):
                hy = "-" + suffix[1:]
                patterns.extend([
                    f"https://sporthink.sm.mncdn.com/mnresize/{s}/{s}/sporthink/uploads/products/original_{sku_variant}{hy}.jpeg",
                    f"https://sporthink.sm.mncdn.com/mnresize/{s}/{s}/sporthink/uploads/products/original_{sku_variant}{hy}.jpg",
                    f"https://sporthink.sm.mncdn.com/sporthink/uploads/products/original_{sku_variant}{hy}.jpeg",
                    f"https://sporthink.sm.mncdn.com/sporthink/uploads/products/original_{sku_variant}{hy}.jpg",
                ])
    candidates = patterns
    # De-duplicate while preserving order
    seen = set()
    out = []
    for u in candidates:
        if u not in seen:
            seen.add(u)
            out.append(u)
    return out

def _resolve_image_for_sku(sku: str, size: int = 300) -> str | None:
    key = (sku or "").strip().upper()
    if not key:
        return None
    if key in _image_cache:
        return _image_cache[key]
    for u in _cdn_candidates_for_sku(key, size=size):
        if _http_exists(u):
            _image_cache[key] = u
            return u
    _image_cache[key] = None
    return None

def _local_image_url_for_sku(sku: str) -> str | None:
    sk = (str(sku or "").strip().upper())
    if not sk:
        return None
    for ext in (".jpeg", ".jpg", ".png", ".svg"):
        p = IMAGES_DIR / f"{sk}{ext}"
        if p.is_file():
            # Served by StaticFiles mount at /static
            return f"/static/images/{p.name}"
    return None

def _product_page_for_sku(sku: str) -> str | None:
    sk = (sku or "").strip()
    if not sk:
        return None
    q = urllib.parse.quote_plus(sk)
    return f"https://www.sporthink.com.tr/arama?q={q}"


def _resolve_any_image(sku: str, size: int = 300) -> str | None:
    """Resolve best image URL for a SKU without writing to disk."""
    key = (sku or "").strip().upper()
    if not key:
        return None
    if key in _img_resolve_cache:
        return _img_resolve_cache[key]
    # Lazy-load raw URL cache if startup hadn't run yet
    if not _sku_raw_url_cache:
        try:
            _sku_raw_url_cache.update(_load_raw_image_links())
        except Exception:
            pass
    # 0) Raw URL from data file (most authoritative Sporthink URL)
    raw_url = _sku_raw_url_cache.get(key)
    if raw_url and raw_url.startswith("http") and str(raw_url).strip() not in {"0", "nan", "none", ""}:
        # Fix broken CDN domain from Excel
        raw_url = raw_url.replace("sporthink.mncdn.com", "sporthink.sm.mncdn.com")
        _img_resolve_cache[key] = raw_url
        return raw_url
    # 1) Hardcoded internal ID map
    if key in _GORSEL_ID_MAP:
        url = f"https://sporthink.sm.mncdn.com/mnresize/{size}/{size}/sporthink/uploads/products/original_{_GORSEL_ID_MAP[key]}_1.jpeg"
        _img_resolve_cache[key] = url
        return url
    # 2) Top CDN SKU-based candidates (limited, fast)
    for u in _cdn_candidates_for_sku(key, size)[:6]:
        if _http_exists(u):
            _img_resolve_cache[key] = u
            return u
    # 3) DDG: Sporthink-specific search (ürün Sporthink CDN'inden gelsin)
    brand, desc = _sku_meta_cache.get(key, ("", ""))
    if not brand:
        brand = _guess_brand_from_sku(key)
    base_q = f"{brand} {desc}".strip() if (brand or desc) else key
    sporthink_img = _discover_image_via_ddg(
        f"{base_q} sporthink.com.tr",
        preferred_domains=["sporthink.sm.mncdn.com", "sporthink"],
        timeout=5.0,
    )
    if sporthink_img:
        _img_resolve_cache[key] = sporthink_img
        return sporthink_img
    # 4) DDG: brand-domain fallback
    brand_img = _discover_image_via_ddg(base_q, brand=brand, timeout=5.0)
    if brand_img:
        _img_resolve_cache[key] = brand_img
        return brand_img
    _img_resolve_cache[key] = None
    return None


@app.get("/api/image/{sku}")
def image_proxy(sku: str, size: int = Query(300, ge=64, le=800)):
    """Redirect to the best available image for a SKU. No disk writes."""
    url = _resolve_any_image(sku.strip(), size=size)
    if url:
        return RedirectResponse(url=url, status_code=302)
    svg = (
        "<svg xmlns='http://www.w3.org/2000/svg' width='300' height='300' viewBox='0 0 300 300'>"
        "<rect width='300' height='300' fill='#1e293b' rx='12'/>"
        "<text x='150' y='145' dominant-baseline='middle' text-anchor='middle' font-family='sans-serif' font-size='14' fill='#475569'>Görsel</text>"
        "<text x='150' y='165' dominant-baseline='middle' text-anchor='middle' font-family='sans-serif' font-size='14' fill='#475569'>Bulunamadı</text>"
        "</svg>"
    )
    return Response(content=svg, media_type="image/svg+xml", headers={"Cache-Control": "no-cache"})

def _download_image(url: str, dest_path: Path, timeout: float = 5.0) -> bool:
    try:
        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": "Mozilla/5.0",
                "Referer": "https://www.sporthink.com.tr/",
                "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
            },
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = resp.read()
        dest_path.parent.mkdir(parents=True, exist_ok=True)
        with open(dest_path, "wb") as f:
            f.write(data)
        return True
    except Exception:
        return False

def _load_raw_image_links() -> dict[str, str]:
    path = resolve_data_path()
    mapping: dict[str, str] = {}
    try:
        if path.suffix.lower() == ".csv":
            raw = pd.read_csv(path)
        else:
            xls = pd.ExcelFile(path, engine="openpyxl")
            target_sheet = "Data" if "Data" in xls.sheet_names else 0
            raw = pd.read_excel(xls, sheet_name=target_sheet)
    except Exception:
        return mapping
    sku_col = None
    for c in ["Stok Kodu", "STOK KODU", "stok kodu", "SKU", "sku"]:
        if c in raw.columns:
            sku_col = c
            break
    if not sku_col:
        return mapping
    url_col = None
    for c in ["Görsel Link", "GÖRSEL LİNK", "Resim", "RESİM", "Image", "IMAGE", "gorsel_link"]:
        if c in raw.columns:
            url_col = c
            break
    if not url_col:
        return mapping
    for _, r in raw[[sku_col, url_col]].dropna(how="all").iterrows():
        sku = _txt_clean(str(r.get(sku_col, "")))
        url = _txt_clean(str(r.get(url_col, "")))
        if not sku or not url:
            continue
        u = url.strip()
        if not u or u.lower() in {"nan", "none", "null"}:
            continue
        key = sku.upper()
        if key not in mapping:
            mapping[key] = u
    return mapping

def _discover_cdn_via_search(sku: str, size: int = 300, timeout: float = 2.0) -> str | None:
    key = (sku or "").strip()
    if not key:
        return None
    try:
        q = urllib.parse.quote_plus(key)
        search_url = f"https://www.sporthink.com.tr/arama?q={q}"
        req = urllib.request.Request(
            search_url,
            headers={
                "User-Agent": "Mozilla/5.0",
                "Referer": "https://www.sporthink.com.tr/",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
            },
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            charset = "utf-8"
            try:
                ct = resp.headers.get("Content-Type", "")
                if "charset=" in ct:
                    charset = ct.split("charset=")[-1].split(";")[0].strip()
            except Exception:
                pass
        html = raw.decode(charset or "utf-8", errors="ignore")
        m = re.search(r"original[_-]([0-9]{4,})[_-]?(?:1)?\.(jpeg|jpg|png)", html, re.IGNORECASE)
        if m:
            pid = m.group(1)
            ext = m.group(2).lower()
            s = int(size)
            return f"https://sporthink.sm.mncdn.com/mnresize/{s}/{s}/sporthink/uploads/products/original_{pid}_1.{ext}"
        m2 = re.search(r"https?://[^\s\"']+original[^\s\"']+\.(?:jpeg|jpg|png)", html, re.IGNORECASE)
        if m2:
            return m2.group(0)
    except Exception:
        return None
    return None

def _brand_cdn_candidates(sku: str, brand: str) -> list[str]:
    """Direct CDN URL patterns for known sports brands."""
    bl = (brand or "").lower()
    ku = (sku or "").strip().upper()
    urls = []
    if "adidas" in bl:
        urls += [
            f"https://assets.adidas.com/images/h_840,f_auto,q_auto,fl_lossy,c_fill,g_auto/{ku}_01_standard.jpg",
            f"https://assets.adidas.com/images/h_840,f_auto,q_auto,fl_lossy,c_fill,g_auto/{ku}_01_standard_hover.jpg",
            f"https://assets.adidas.com/images/w_600,f_auto,q_auto/{ku}_01_standard.jpg",
        ]
    if "puma" in bl:
        urls += [
            f"https://images.puma.com/image/upload/f_auto,q_auto,b_rgb:fafafa,w_600/{ku}.jpg",
            f"https://images.puma.com/image/upload/f_auto,q_auto,w_600/{ku}.jpg",
        ]
    if "vans" in bl:
        urls += [
            f"https://images.vans.com/is/image/VansEU/{ku}-HERO?$SCALE-ORIGINAL$",
            f"https://images.vans.com/is/image/VansEU/{ku}?$pdpflexf2$",
        ]
    if "new balance" in bl:
        urls += [
            f"https://nb.scene7.com/is/image/NB/{ku}?$pdpflexf2$",
            f"https://nb.scene7.com/is/image/NB/{ku}_nb_02_i?$pdpflexf2$",
        ]
    if "columbia" in bl:
        urls += [
            f"https://columbia.scene7.com/is/image/ColumbiaSportswear2/{ku}_001?$pdp-md-opt$",
        ]
    return urls


_BRAND_SEARCH_URLS: dict[str, str] = {
    "adidas": "https://www.adidas.com.tr/arama?q={q}",
    "nike": "https://www.nike.com/tr/search?q={q}",
    "puma": "https://tr.puma.com/search?q={q}",
    "new balance": "https://www.newbalance.com.tr/search?q={q}",
    "hummel": "https://www.hummel.net/tr/tr/search?q={q}",
    "vans": "https://www.vans.com.tr/search?q={q}",
    "the north face": "https://www.thenorthface.com.tr/search?q={q}",
    "reebok": "https://www.reebok.com.tr/search?q={q}",
    "columbia": "https://www.columbia.com/search?q={q}",
    "under armour": "https://www.underarmour.com.tr/tr-TR/search?q={q}",
    "asics": "https://www.asics.com/tr/tr-tr/as-search?q={q}",
    "converse": "https://www.converse.com.tr/search?q={q}",
    "salomon": "https://www.salomon.com/tr-tr/search?q={q}",
    "wilson": "https://www.wilson.com/en-us/search?q={q}",
    "head": "https://www.head.com/en-US/sports/tennis/search?q={q}",
}


def _scrape_og_image(url: str, timeout: float = 3.0) -> str | None:
    """Fetch a page and extract the og:image meta tag."""
    try:
        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
                "Accept-Language": "tr-TR,tr;q=0.9,en-US;q=0.7,en;q=0.6",
            },
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            html = resp.read().decode("utf-8", errors="ignore")
        # og:image property
        for pat in [
            r'<meta[^>]+property=["\']og:image["\'][^>]*content=["\']([^"\'>]+)["\']',
            r'<meta[^>]*content=["\']([^"\'>]+)["\'][^>]*property=["\']og:image["\']',
        ]:
            m = re.search(pat, html, re.IGNORECASE)
            if m:
                u = m.group(1).strip()
                if u.startswith("http"):
                    return u
        # JSON imageUrl
        m2 = re.search(r'["\']imageUrl["\']\s*:\s*["\']([^"\'>]+\.(?:jpg|jpeg|png|webp))["\']', html, re.IGNORECASE)
        if m2:
            u = m2.group(1).replace("\\/", "/")
            if u.startswith("http"):
                return u
    except Exception:
        return None
    return None


def _guess_brand_from_sku(sku: str) -> str:
    """Best-effort brand detection from SKU pattern when meta cache is not yet populated."""
    ku = (sku or "").strip().upper()
    # Vans: VN...
    if ku.startswith("VN"):
        return "vans"
    # The North Face: NF...
    if ku.startswith("NF"):
        return "the north face"
    # Adidas: 2 letters + 4 digits (e.g. JM6535, IX3178) or similar
    if re.match(r'^[A-Z]{2}\d{4}$', ku) or re.match(r'^[A-Z]{2,3}\d{4,5}$', ku):
        return "adidas"
    # Hummel: 3 digits + hyphen + pattern (e.g. 980234-2001)
    if re.match(r'^\d{6}-\d{4}$', ku):
        return "hummel"
    return ""


_DDG_BRAND_DOMAINS: dict[str, list[str]] = {
    "adidas": ["adidas.com", "assets.adidas"],
    "nike": ["nike.com"],
    "puma": ["puma.com"],
    "hummel": ["hummel.net"],
    "vans": ["vans.com"],
    "new balance": ["newbalance.com"],
    "the north face": ["thenorthface.com"],
    "under armour": ["underarmour.com"],
    "reebok": ["reebok.com"],
    "columbia": ["columbia.com"],
    "asics": ["asics.com"],
    "converse": ["converse.com"],
    "salomon": ["salomon.com"],
    "wilson": ["wilson.com"],
}


def _discover_image_via_ddg(
    query: str,
    brand: str = "",
    preferred_domains: list[str] | None = None,
    timeout: float = 5.0,
) -> str | None:
    """Search DuckDuckGo Images, preferring specified domains then brand domains."""
    q = (query or "").strip()
    if not q:
        return None
    try:
        req1 = urllib.request.Request(
            f"https://duckduckgo.com/?q={urllib.parse.quote_plus(q)}&ia=images",
            headers={
                "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.9",
            },
        )
        with urllib.request.urlopen(req1, timeout=timeout) as r:
            html = r.read().decode("utf-8", errors="ignore")
        m = re.search(r'vqd=["\']([\.\d-]+)["\']', html)
        if not m:
            m = re.search(r'vqd=([\.\d-]+)', html)
        if not m:
            return None
        vqd = m.group(1)
        img_api = (
            f"https://duckduckgo.com/i.js"
            f"?q={urllib.parse.quote_plus(q)}"
            f"&vqd={urllib.parse.quote_plus(vqd)}"
            f"&f=,,,,,&p=1&o=json"
        )
        req2 = urllib.request.Request(
            img_api,
            headers={
                "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                "Accept": "application/json, */*",
                "Referer": "https://duckduckgo.com/",
            },
        )
        with urllib.request.urlopen(req2, timeout=timeout) as r2:
            data = json.loads(r2.read().decode("utf-8", errors="ignore"))
        results = data.get("results", [])
        if not results:
            return None
        # Build priority domain list: explicit > brand map
        priority = list(preferred_domains or [])
        if not priority:
            bl = (brand or "").lower()
            for bk, domains in _DDG_BRAND_DOMAINS.items():
                if bk in bl:
                    priority = domains
                    break
        if priority:
            for r in results[:20]:
                img = r.get("image", "")
                if img and any(d in img for d in priority):
                    return img
        # Fallback: first usable image
        for r in results[:5]:
            img = r.get("image", "")
            if img and img.startswith("http"):
                return img
    except Exception:
        return None
    return None


def _discover_image_brand_site(sku: str, brand: str, desc: str, timeout: float = 3.0) -> str | None:
    """Resolve product image from brand's official CDN or search page."""
    bl = (brand or "").lower()
    ku = (sku or "").strip().upper()
    if not bl:
        bl = _guess_brand_from_sku(ku)
    # 1) Direct brand CDN
    for url in _brand_cdn_candidates(ku, bl):
        if _http_exists(url):
            return url
    # 2) Brand search page → og:image
    query = urllib.parse.quote_plus(ku)
    for brand_key, tmpl in _BRAND_SEARCH_URLS.items():
        if brand_key in bl:
            img = _scrape_og_image(tmpl.format(q=query), timeout=timeout)
            if img:
                return img
            break
    return None

def _ensure_placeholder_svg() -> Path:
    p = IMAGES_DIR / "_placeholder.svg"
    if not p.exists():
        svg = (
            "<svg xmlns='http://www.w3.org/2000/svg' width='300' height='300'>"
            "<rect width='100%' height='100%' fill='#e2e8f0'/><text x='50%' y='50%' dominant-baseline='middle' text-anchor='middle' font-size='18' fill='#64748b'>No Image</text>"
            "</svg>"
        )
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            f.write(svg)
    return p
    sku_col = None
    for c in ["Stok Kodu", "STOK KODU", "stok kodu", "SKU", "sku"]:
        if c in raw.columns:
            sku_col = c
            break
    if not sku_col:
        return mapping
    url_col = None
    for c in ["Görsel Link", "GÖRSEL LİNK", "Resim", "RESİM", "Image", "IMAGE", "gorsel_link"]:
        if c in raw.columns:
            url_col = c
            break
    if not url_col:
        return mapping
    # Build mapping with first non-empty per SKU
    for _, r in raw[[sku_col, url_col]].dropna(how="all").iterrows():
        sku = _txt_clean(str(r.get(sku_col, "")))
        url = _txt_clean(str(r.get(url_col, "")))
        if not sku or not url:
            continue
        u = url.strip()
        if not u or u.lower() in {"nan", "none", "null"}:
            continue
        key = sku.upper()
        if key not in mapping:
            mapping[key] = u
    return mapping

def _load_and_process() -> tuple[pd.DataFrame, str]:
    path = resolve_data_path()
    mtime = os.path.getmtime(path)
    
    if _cache.get("path") == path and _cache.get("mtime") == mtime:
        return _cache["data"].copy(), _cache["filename"]

    suffix = path.suffix.lower()
    if suffix == ".csv":
        try:
            df = pd.read_csv(path, encoding="utf-8-sig")
        except UnicodeDecodeError:
            df = pd.read_csv(path, encoding="latin-1")
    elif suffix in (".xlsx", ".xlsm"):
        # Veritabanında (Excel'de) birden fazla sayfa olabilir. Asıl veriler genellikle 'Data' sayfasındadır.
        try:
            xls = pd.ExcelFile(path, engine="openpyxl")
            target_sheet = "Data" if "Data" in xls.sheet_names else 0
            df = pd.read_excel(xls, sheet_name=target_sheet, dtype={"Stok Kodu": str, "E-TİCARET RENK": str})
        except Exception:
            df = pd.read_excel(path, engine="openpyxl", dtype={"Stok Kodu": str, "E-TİCARET RENK": str})
    else:
        raise HTTPException(
            status_code=400,
            detail=f"Desteklenmeyen dosya uzantısı: {suffix}",
        )

    df = _normalize_columns(df)

    # ═══════════════════════════════════════════════════════════════════════
    # VERİ TEMİZLEME VE DÜZENLEME SÜRECİ
    # ═══════════════════════════════════════════════════════════════════════

    # 1. TEKİLLEŞTİRME (Deduplication)
    df = _remove_duplicates(df)

    if _is_summary_export(df):
        out = _process_summary_export(df)
        return out, path.name

    missing = [c for c in EXPECTED_COLUMNS if c not in df.columns]
    if missing:
        raise HTTPException(
            status_code=400,
            detail=f"Veride eksik sütunlar: {missing}",
        )

    # 2. SAYISAL DÖNÜŞÜM (Numeric Transformation)
    # "15 adet", "0,0", "1.234,56" gibi metinsel ifadeleri temizle
    num_cols = [
        "Alış Miktarı",
        "Alış Tutarı",
        "Satış Miktarı",
        "Satış Tutarı",
        "DSS Miktar",
        "Ortalama Stok",
        "Alış Fiyatı",
        "Alış Fiyat",
        "PSF",
        "Ciro",
        "Toplam Kar",
        "Toplam SMM",
        "Toplam Initial Ciro",
    ]
    df = _clean_numeric_values(df, num_cols)

    # 3. STANDARDİZASYON (Standardization)
    # Marka isimlerini birleştir: "adidas", "ADIDAS", " Adidas " → "Adidas"
    df = _standardize_brand_names(df, "Marka Açıklama")

    # Metin temizliği
    if "Marka Açıklama" in df.columns:
        df["Marka Açıklama"] = df["Marka Açıklama"].map(_txt_clean)

    for col in GROUP_COLS:
        if col in df.columns:
            df[col] = df[col].map(_txt_clean)
    for col in ["E-TİCARET RENK", "Renk Açıklama", "E-TİCARET CİNSİYET", "E-TİCARET MEVSİM"]:
        if col in df.columns:
            df[col] = df[col].map(_txt_clean)

    # 4. HATA YÖNETİMİ (Error Handling)
    # Sıfıra bölme hatalarını önle
    df = _handle_division_by_zero(df)

    # Sayısal kolonlarda NaN'ları 0 yap
    for c in num_cols:
        if c in df.columns:
            df[c] = df[c].fillna(0)

    out = _aggregate_transactional(df)
    out = _apply_kpi_rules(out, VAT_DEFAULT)

    # Sıfır stok flag'ini kullanarak Sell Through'u düzelt
    if '_zero_stock_flag' in out.columns:
        out.loc[out['_zero_stock_flag'] == True, 'sell_through_pct'] = 0.0
        out = out.drop(columns=['_zero_stock_flag'])

    # gorsel_link: _process_summary_export zaten Excel'den kopyalar.
    # Ham veriler (transactional) için apply_category_mappings içinde _get_gorsel_link çağrılır.
    # Burada sıfırlama YOK — mevcut değerler korunur.
    if "gorsel_link" not in out.columns:
        out["gorsel_link"] = ""

    
    out = _apply_category_mappings(out)
    
    _cache["path"] = path
    _cache["mtime"] = mtime
    _cache["data"] = out
    _cache["filename"] = path.name
    
    return out.copy(), path.name


@app.get("/api/dashboard")
def dashboard(
    view: str = Query("all", description="all, best, veya worst"),
    cinsiyet: str | None = Query(None, description="CİNSİYET filtresi; boş = tümü"),
    anagrup: str | None = Query(None, description="ANAGRUP filtresi; boş = tümü"),
    alt_kategori: str | None = Query(None, description="Alt Kategori filtresi; boş = tümü"),
    renk: str | None = Query(None, description="E-TİCARET RENK filtresi; boş = tümü"),
):
    metrics, data_filename = _load_and_process()
    
    result: dict = {"meta": {"data_file": data_filename}}

    if view in ("all", "best"):
        bs_data = get_bestsellers(
            metrics,
            cinsiyet=cinsiyet,
            anagrup=anagrup,
            alt_kategori=alt_kategori,
            renk=renk,
        )
        result["bestsellers"] = bs_data["items"]
        result["best_filters"] = bs_data["filters"]

    if view in ("all", "worst"):
        ws_data = get_worstsellers(
            metrics,
            cinsiyet=cinsiyet,
            anagrup=anagrup,
            alt_kategori=alt_kategori,
            renk=renk,
        )
        result["worstsellers"] = ws_data["items"]
        result["worst_filters"] = ws_data["filters"]

    return result

def _apply_category_mappings(df: pd.DataFrame) -> pd.DataFrame:
    """Alt Kategori'den Ürün Grubu ve Ana Kategori türetir."""
    df = df.copy()

    def _urun_grubu(alt_kat: str) -> str:
        s = str(alt_kat).lower()
        if "günlük ayakkabı" in s:                return "Günlük Ayakkabı"
        if "sneaker" in s:                        return "Sneaker"
        if "spor ayakkabı" in s:                  return "Spor Ayakkabı"
        if "koşu ayakkabı" in s or "koşu ayakkabısı" in s: return "Koşu Ayakkabısı"
        if "outdoor ayakkabı" in s or "outdoor ayakkabısı" in s: return "Outdoor Ayakkabı"
        if "bot" in s or "çizme" in s:            return "Bot ve Çizme"
        if "halı saha ayakkabı" in s or "halı saha ayakkabısı" in s: return "Halı Saha Ayakkabısı"
        if "krampon" in s:                        return "Krampon"
        if "basketbol ayakkabı" in s or "basketbol ayakkabısı" in s: return "Basketbol Ayakkabısı"
        if "voleybol ayakkabı" in s or "voleybol ayakkabısı" in s: return "Voleybol Ayakkabısı"
        if "tenis ayakkabı" in s or "tenis ayakkabısı" in s: return "Tenis Ayakkabısı"
        if "sandalet" in s:                       return "Sandalet"
        if "terlik" in s:                         return "Terlik"
        if "ayakkabı" in s:                       return "Ayakkabı"
        if "sırt çanta" in s or "sırt çantası" in s: return "Sırt Çantası"
        if "omuz çanta" in s or "omuz çantası" in s: return "Omuz Çantası"
        if "bel çanta" in s or "bel çantası" in s: return "Bel Çantası"
        if "spor çanta" in s or "spor çantası" in s: return "Spor Çantası"
        if "el çanta" in s or "el çantası" in s:  return "El Çantası"
        if "bayan çanta" in s or "bayan çantası" in s: return "El Çantası"
        if "valiz" in s:                          return "Valiz"
        if "çanta" in s:                          return "Çanta"
        if "basketbol topu" in s:                 return "Basketbol Topu"
        if "futbol topu" in s:                    return "Futbol Topu"
        if "pilates topu" in s:                   return "Pilates Topu"
        if "voleybol topu" in s:                  return "Voleybol Topu"
        if "bone" in s:                           return "Bone"
        if "tekmelik" in s:                       return "Tekmelik"
        if "fitness eldiven" in s:                return "Fitness Eldiveni"
        if "kaleci eldiven" in s:                 return "Kaleci Eldiveni"
        if "yoga mat" in s:                       return "Yoga Matı"
        if "yüzücü" in s or "gözlük" in s:        return "Yüzücü Gözlüğü"
        if "eşofman" in s:                        return "Eşofman"
        if "sweatshirt" in s:                     return "Sweatshirt"
        if "tişört" in s or "tshirt" in s:        return "Tişört"
        if "gömlek" in s:                         return "Gömlek"
        if "jean" in s:                           return "Jean"
        if "pantolon" in s:                       return "Pantolon"
        if "bluz" in s:                           return "Bluz"
        if "atlet" in s:                          return "Atlet"
        if "şort" in s:                           return "Şort"
        if "sporcu sütyeni" in s:                 return "Sporcu Sütyeni"
        if "elbise" in s:                         return "Elbise"
        if "forma" in s:                          return "Forma"
        if "kazak" in s:                          return "Kazak"
        if "mayo" in s:                           return "Mayoşort ve Mayo"
        if "mont" in s or "ceket" in s:           return "Mont / Ceket"
        if "yelek" in s:                          return "Yelek"
        if "tayt" in s:                           return "Tayt"
        if "çorap" in s:                          return "Çorap"
        if "tozluk" in s:                         return "Tozluk"
        if "şapka" in s:                          return "Şapka"
        if "kalem kutu" in s:                     return "Kalem Kutusu"
        if "matara" in s:                         return "Matara"
        if "cüzdan" in s:                         return "Cüzdan"
        if "iç çamaşır" in s:                     return "İç Çamaşırı"
        if "eldiven" in s or "bere" in s:         return "Eldiven & Bere"
        if "atkı" in s or "şal" in s:             return "Atkı ve Şal"
        if "kemer" in s:                          return "Kemer"
        if "ayakkabı bakım" in s:                 return "Ayakkabı Bakım Ürünleri"
        if "aksesuar" in s or "ekipman" in s:     return "Aksesuar"
        if "outdoor" in s:                        return "Outdoor"
        parts = str(alt_kat).split(" - ", 1)
        return parts[1].strip() if len(parts) == 2 else str(alt_kat).strip()

    def _ana_kategori(urun_grubu: str) -> str:
        if urun_grubu in {
            "Günlük Ayakkabı", "Sneaker", "Spor Ayakkabı", "Koşu Ayakkabısı",
            "Outdoor Ayakkabı", "Bot ve Çizme", "Halı Saha Ayakkabısı",
            "Krampon", "Basketbol Ayakkabısı", "Voleybol Ayakkabısı",
            "Tenis Ayakkabısı", "Sandalet", "Terlik", "Ayakkabı",
        }:
            return "Ayakkabı"
        if urun_grubu in {
            "Tişört", "Gömlek", "Eşofman", "Jean", "Pantolon", "Bluz",
            "Atlet", "Tayt", "Elbise", "Yelek", "Şort", "Sporcu Sütyeni",
            "Forma", "Kazak", "Mayoşort ve Mayo", "Mont / Ceket", "Sweatshirt",
        }:
            return "Giyim"
        if urun_grubu in {
            "Basketbol Topu", "Futbol Topu", "Pilates Topu", "Voleybol Topu",
            "Bone", "Tekmelik", "Fitness Eldiveni", "Kaleci Eldiveni",
            "Yoga Matı", "Yüzücü Gözlüğü", "Matara", "Outdoor"
        }:
            return "Ekipman"
        return "Aksesuar"

    # Sporthink Stok Kodu -> Internal ID Eşleşmesi (Resimler için)
    gorsel_id_map = {
        "JM6535": "162492", "JN2709": "162509", "ID3807": "158727",
        "JN2708": "162508", "VN000H4WEMJ1": "163021", "NF0A3VXF0IT1": "154848",
        "VN000H4W12B1": "163012", "VN000H4W6671": "163015", "VN000H4W6701": "163016",
        "JI3470": "158369", "JI1545": "158382", "IF4136": "158682",
        "IH2844": "152569", "JI0910": "158388", "ID2855": "148433",
        "JI1710": "158379", "JI1541": "158381", "JH8620": "158378",
        "JW2435": "158390", "JE1188": "158376", "JY3484": "162685",
        "JX3914": "162681", "IK4009": "152778", "JI8960": "161295",
        "JN9930": "160759", "JM3113": "160354"
    }

    def _get_gorsel_link(row):
        sk = str(row.get("Stok Kodu", "")).strip()
        internal_id = gorsel_id_map.get(sk)
        if internal_id:
            return f"https://sporthink.sm.mncdn.com/mnresize/300/300/sporthink/uploads/products/original_{internal_id}_1.jpeg"
        return None

    df["_grup"] = df["Alt Kategori"].apply(_urun_grubu)
    df["_ana_kategori"] = df["_grup"].apply(_ana_kategori)
    df["gorsel_link"] = df.apply(_get_gorsel_link, axis=1)
    # Populate meta cache for Bing fallback
    try:
        desc_col = next((c for c in ["Stok Kodu Açıklama", "Stok Açıklama", "Açıklama"] if c in df.columns), None)
        brand_col = "Marka Açıklama" if "Marka Açıklama" in df.columns else None
        for _, row in df[["Stok Kodu"] + ([brand_col] if brand_col else []) + ([desc_col] if desc_col else [])].iterrows():
            sk = str(row.get("Stok Kodu", "") or "").strip().upper()
            if sk and sk not in _sku_meta_cache:
                brand = str(row.get(brand_col or "", "") or "").strip()
                desc = str(row.get(desc_col or "", "") or "").strip()
                _sku_meta_cache[sk] = (brand, desc)
    except Exception:
        pass
    return df


def _get_sezon_col(df: pd.DataFrame) -> str | None:
    """Sezon verisi içeren kolonu bulur."""
    for col in ["Mevcut Sezon Kodu", "Sezon", "E-TİCARET MEVSİM", "Mevsim"]:
        if col in df.columns:
            return col
    return None


@app.get("/api/dashboard")
def dashboard(
    view: str = Query("all", description="all, best, veya worst"),
    cinsiyet: str | None = Query(None, description="CİNSİYET filtresi; boş = tümü"),
    anagrup: str | None = Query(None, description="ANAGRUP filtresi; boş = tümü"),
    alt_kategori: str | None = Query(None, description="Alt Kategori filtresi; boş = tümü"),
    renk: str | None = Query(None, description="E-TİCARET RENK filtresi; boş = tümü"),
):
    metrics, data_filename = _load_and_process()
    metrics = _apply_category_mappings(metrics)

    option_pool = metrics
    if cinsiyet:
        option_pool = option_pool[option_pool["CİNSİYET"] == cinsiyet]
    anagrup_opt = sorted(option_pool["_ana_kategori"].dropna().unique().tolist())

    ak_pool = option_pool if not anagrup else option_pool[option_pool["_ana_kategori"] == anagrup]
    alt_kategori_opt = sorted(ak_pool["_grup"].dropna().unique().tolist())

    renk_pool = ak_pool if not alt_kategori else ak_pool[ak_pool["_grup"] == alt_kategori]
    renk_opt = sorted(
        renk_pool["E-TİCARET RENK"]
        .dropna()
        .astype(str)
        .map(str.strip)
        .loc[lambda s: s != ""]
        .unique()
        .tolist(),
        key=lambda x: str(x),
    )

    filtered = metrics
    if cinsiyet:
        filtered = filtered[filtered["CİNSİYET"] == cinsiyet]
    if anagrup:
        filtered = filtered[filtered["_ana_kategori"] == anagrup]
    if alt_kategori:
        filtered = filtered[filtered["_grup"] == alt_kategori]
    if renk:
        filtered = filtered[filtered["E-TİCARET RENK"] == renk]

    # ═══════════════════════════════════════════════════════════════════════
    # BESTSELLER / WORSTSELLER — Sell Through % üzerine kurulu algoritma
    # Sell Through % = Satış Miktarı / (Satış Miktarı + DSS Miktar) × 100
    # Bestseller  = Sell Through % en yüksek → en düşük (DESC)
    # Worstseller = Sell Through % en düşük → en yüksek (ASC)
    # ═══════════════════════════════════════════════════════════════════════
    bs = filtered.sort_values(
        ["sell_through_pct", "Satış Miktarı", "Satış Tutarı", "brut_kar"],
        ascending=[False, False, False, False],
        na_position="last",
        kind="mergesort",
    ).head(40)

    ws_pool = _worstseller_pool(filtered)
    ws = _sort_worstsellers(ws_pool).head(10)

    return {
        "bestsellers": [_row_to_product(bs.iloc[i]) for i in range(len(bs))],
        "worstsellers": [_row_to_product(ws.iloc[i]) for i in range(len(ws))],
        "filters": {
            "cinsiyet_options": cinsiyet_opt,
            "anagrup_options": anagrup_opt,
            "alt_kategori_options": alt_kategori_opt,
            "renk_options": renk_opt,
        },
        "meta": {
            "data_file": data_filename,
            "sort_bestseller": "Sell Through % DESC, Satış Miktarı DESC, Ciro DESC, Kar DESC",
            "sort_worstseller": "DSS > 0 ve E-TİCARET RENK dolu, Sell Through % ASC, Satış ASC, DSS DESC",
            "sell_through_formula": "Satış Miktarı / (Satış Miktarı + DSS Miktar) × 100",
            "periyot_cover_formula": "(DSS Miktar / Satış Miktarı) × 19; Satış 0 ise 1000",
        },
    }


def get_bestsellers(
    metrics: pd.DataFrame,
    cinsiyet: str | None = None,
    anagrup: str | None = None,
    alt_kategori: str | None = None,
    renk: str | None = None,
    sezon_filter: str | None = None,
) -> dict:
    """Bestseller listesini ve filtre seçeneklerini hesaplar."""
    # Filtre seçeneklerini hesapla (başlangıç havuzuna göre)
    cinsiyet_opt = sorted(metrics["CİNSİYET"].dropna().unique().tolist(), key=lambda x: str(x))
    anagrup_opt = sorted(metrics["_ana_kategori"].dropna().unique().tolist())
    alt_kategori_opt = sorted(metrics["_grup"].dropna().unique().tolist())
    renk_opt = sorted(
        metrics["E-TİCARET RENK"].dropna().astype(str).map(str.strip).loc[lambda s: s != ""].unique().tolist(),
        key=lambda x: str(x),
    )
    
    sezon_col = _get_sezon_col(metrics)
    sezon_opt = sorted(metrics[sezon_col].dropna().astype(str).unique().tolist()) if sezon_col else []

    # Filtrele
    filtered = metrics
    if sezon_filter and sezon_col:
        filtered = filtered[filtered[sezon_col] == sezon_filter]
        cinsiyet_opt = sorted(filtered["CİNSİYET"].dropna().unique().tolist(), key=lambda x: str(x))
    if cinsiyet:
        filtered = filtered[filtered["CİNSİYET"] == cinsiyet]
        anagrup_opt = sorted(filtered["_ana_kategori"].dropna().unique().tolist())
    if anagrup:
        filtered = filtered[filtered["_ana_kategori"] == anagrup]
        alt_kategori_opt = sorted(filtered["_grup"].dropna().unique().tolist())
    if alt_kategori:
        filtered = filtered[filtered["_grup"] == alt_kategori]
        renk_opt = sorted(
            filtered["E-TİCARET RENK"].dropna().astype(str).map(str.strip).loc[lambda s: s != ""].unique().tolist(),
            key=lambda x: str(x),
        )
    if renk:
        filtered = filtered[filtered["E-TİCARET RENK"] == renk]

    # Sıralama
    bs = filtered.sort_values(
        ["sell_through_pct", "Satış Miktarı", "Satış Tutarı", "brut_kar"],
        ascending=[False, False, False, False],
        na_position="last",
        kind="mergesort",
    ).head(10)

    items = [_row_to_product(bs.iloc[i]) for i in range(len(bs))]

    return {
        "items": items,
        "filters": {
            "cinsiyet_options": cinsiyet_opt,
            "anagrup_options": anagrup_opt,
            "alt_kategori_options": alt_kategori_opt,
            "renk_options": renk_opt,
            "sezon_options": sezon_opt,
        }
    }


def get_worstsellers(
    metrics: pd.DataFrame,
    cinsiyet: str | None = None,
    anagrup: str | None = None,
    alt_kategori: str | None = None,
    renk: str | None = None,
    sezon_filter: str | None = None,
) -> dict:
    """Worstseller listesini ve filtre seçeneklerini hesaplar."""
    # Worstseller havuzunu al (DSS > 0 olanlar, Satış > 0 olanlar)
    ws_pool_all = _worstseller_pool(metrics)

    # Filtre seçeneklerini hesapla (worstseller havuzuna göre)
    cinsiyet_opt = sorted(ws_pool_all["CİNSİYET"].dropna().unique().tolist(), key=lambda x: str(x))
    anagrup_opt = sorted(ws_pool_all["_ana_kategori"].dropna().unique().tolist())
    alt_kategori_opt = sorted(ws_pool_all["_grup"].dropna().unique().tolist())
    renk_opt = sorted(
        ws_pool_all["E-TİCARET RENK"].dropna().astype(str).map(str.strip).loc[lambda s: s != ""].unique().tolist(),
        key=lambda x: str(x),
    )
    
    sezon_col = _get_sezon_col(metrics)
    sezon_opt = sorted(ws_pool_all[sezon_col].dropna().astype(str).unique().tolist()) if sezon_col else []

    # Filtrele
    filtered_ws = ws_pool_all
    if sezon_filter and sezon_col:
        filtered_ws = filtered_ws[filtered_ws[sezon_col] == sezon_filter]
        cinsiyet_opt = sorted(filtered_ws["CİNSİYET"].dropna().unique().tolist(), key=lambda x: str(x))
    if cinsiyet:
        filtered_ws = filtered_ws[filtered_ws["CİNSİYET"] == cinsiyet]
        anagrup_opt = sorted(filtered_ws["_ana_kategori"].dropna().unique().tolist())
    if anagrup:
        filtered_ws = filtered_ws[filtered_ws["_ana_kategori"] == anagrup]
        alt_kategori_opt = sorted(filtered_ws["_grup"].dropna().unique().tolist())
    if alt_kategori:
        filtered_ws = filtered_ws[filtered_ws["_grup"] == alt_kategori]
        renk_opt = sorted(
            filtered_ws["E-TİCARET RENK"].dropna().astype(str).map(str.strip).loc[lambda s: s != ""].unique().tolist(),
            key=lambda x: str(x),
        )
    if renk:
        filtered_ws = filtered_ws[filtered_ws["E-TİCARET RENK"] == renk]

    # Sıralama
    ws = _sort_worstsellers(filtered_ws).head(10)

    items = [_row_to_product(ws.iloc[i]) for i in range(len(ws))]

    return {
        "items": items,
        "filters": {
            "cinsiyet_options": cinsiyet_opt,
            "anagrup_options": anagrup_opt,
            "alt_kategori_options": alt_kategori_opt,
            "renk_options": renk_opt,
            "sezon_options": sezon_opt,
        }
    }


@app.get("/api/dashboard/bestseller")
def dashboard_bestseller(
    cinsiyet: str | None = Query(None, description="CİNSİYET filtresi; boş = tümü"),
    anagrup: str | None = Query(None, description="ANAGRUP filtresi; boş = tümü"),
    alt_kategori: str | None = Query(None, description="Alt Kategori filtresi; boş = tümü"),
    renk: str | None = Query(None, description="E-TİCARET RENK filtresi; boş = tümü"),
    sezon: str | None = Query(None, description="Sezon filtresi; boş = tümü"),
):
    """Sadece Bestseller listesi - kendi filtre seçenekleriyle."""
    metrics, data_filename = _load_and_process()
    metrics = _apply_category_mappings(metrics)

    res = get_bestsellers(
        metrics, 
        cinsiyet=cinsiyet, 
        anagrup=anagrup, 
        alt_kategori=alt_kategori, 
        renk=renk, 
        sezon_filter=sezon
    )

    return {
        "bestsellers": res["items"],
        "filters": res["filters"],
        "meta": {
            "data_file": data_filename,
            "type": "bestseller",
            "sort": "Sell Through % DESC, Satış Miktarı DESC, Ciro DESC, Kar DESC",
        },
    }


@app.get("/api/dashboard/worstseller")
def dashboard_worstseller(
    cinsiyet: str | None = Query(None, description="CİNSİYET filtresi; boş = tümü"),
    anagrup: str | None = Query(None, description="ANAGRUP filtresi; boş = tümü"),
    alt_kategori: str | None = Query(None, description="Alt Kategori filtresi; boş = tümü"),
    renk: str | None = Query(None, description="E-TİCARET RENK filtresi; boş = tümü"),
    sezon: str | None = Query(None, description="Sezon filtresi; boş = tümü"),
):
    """Sadece Worstseller listesi - kendi filtre seçenekleriyle."""
    metrics, data_filename = _load_and_process()
    metrics = _apply_category_mappings(metrics)

    res = get_worstsellers(
        metrics, 
        cinsiyet=cinsiyet, 
        anagrup=anagrup, 
        alt_kategori=alt_kategori, 
        renk=renk, 
        sezon_filter=sezon
    )

    return {
        "worstsellers": res["items"],
        "filters": res["filters"],
        "meta": {
            "data_file": data_filename,
            "type": "worstseller",
            "sort": "Sell Through % ASC, DSS DESC",
        },
    }


@app.get("/api/top10")
def top10(
    cinsiyet: str | None = Query(None, description="CİNSİYET filtresi; boş = tümü"),
    alt_kategori: str | None = Query(None, description="Alt Kategori filtresi; boş = tümü"),
    renk: str | None = Query(None, description="E-TİCARET RENK filtresi; boş = tümü"),
    view: str = Query("best", description="best veya worst"),
    limit: int = Query(10, ge=1, le=50, description="Kaç ürün dönecek"),
):
    """
    Büyük görselli ekran için: Cinsiyet × Alt Kategori segmentinde Top 10.
    Sell Through % üzerine kurulu sıralama.
    """
    metrics, data_filename = _load_and_process()

    filtered = metrics
    if cinsiyet:
        filtered = filtered[filtered["CİNSİYET"] == cinsiyet]
    if alt_kategori:
        filtered = filtered[filtered["Alt Kategori"] == alt_kategori]
    if renk:
        filtered = filtered[filtered["E-TİCARET RENK"] == renk]

    if view == "best":
        result = filtered.sort_values(
            ["sell_through_pct", "Satış Miktarı", "Satış Tutarı", "brut_kar"],
            ascending=[False, False, False, False],
            na_position="last",
            kind="mergesort",
        ).head(limit)
    else:
        ws_pool = _worstseller_pool(filtered)
        result = _sort_worstsellers(ws_pool).head(limit)

    items = [_row_to_product(result.iloc[i]) for i in range(len(result))]

    return {
        "items": items,
        "filters_applied": {
            "cinsiyet": cinsiyet,
            "alt_kategori": alt_kategori,
            "renk": renk,
        },
        "view": view,
        "meta": {
            "data_file": data_filename,
            "total_matching": len(filtered),
        },
    }


@app.get("/api/debug/worstseller-rules")
def debug_worstseller_rules(
    cinsiyet: str | None = Query(None),
    anagrup: str | None = Query(None),
    alt_kategori: str | None = Query(None),
    limit: int = Query(15, ge=1, le=50),
):
    metrics, data_filename = _load_and_process()
    filtered = metrics
    if cinsiyet:
        filtered = filtered[filtered["CİNSİYET"] == cinsiyet]
    if anagrup:
        filtered = filtered[filtered["ANAGRUP"] == anagrup]
    if alt_kategori:
        filtered = filtered[filtered["Alt Kategori"] == alt_kategori]

    pool = _worstseller_pool(filtered)
    rules = {
        "st_asc_satis_asc_dss_desc": (
            ["sell_through_pct", "Satış Miktarı", "DSS Miktar"],
            [True, True, False],
        ),
        "st_asc_cover_desc_dss_desc": (
            ["sell_through_pct", "periyot_cover_19", "DSS Miktar"],
            [True, False, False],
        ),
        "st_asc_dss_desc": (
            ["sell_through_pct", "DSS Miktar"],
            [True, False],
        ),
        "st_asc_satis_asc_cover_desc_dss_desc": (
            ["sell_through_pct", "Satış Miktarı", "periyot_cover_19", "DSS Miktar"],
            [True, True, False, False],
        ),
        "st_asc_satis_asc_dss_asc": (
            ["sell_through_pct", "Satış Miktarı", "DSS Miktar"],
            [True, True, True],
        ),
        "cover_desc_only": (
            ["periyot_cover_19"],
            [False],
        ),
    }

    return {
        "data_file": data_filename,
        "pool_count": len(pool),
        "filters_applied": {
            "cinsiyet": cinsiyet,
            "anagrup": anagrup,
            "alt_kategori": alt_kategori,
        },
        "rules": {
            name: [
                _row_to_product(row)
                for _, row in pool.sort_values(
                    cols,
                    ascending=asc,
                    na_position="last",
                    kind="mergesort",
                ).head(limit).iterrows()
            ]
            for name, (cols, asc) in rules.items()
        },
    }


@app.get("/api/debug/worstseller-match")
def debug_worstseller_match(
    expected: str = Query(..., description="Excel'deki doğru Stok Kodu sırası, virgülle ayrılmış"),
    cinsiyet: str | None = Query(None),
    anagrup: str | None = Query(None),
    alt_kategori: str | None = Query(None),
    limit: int = Query(10, ge=1, le=50),
):
    metrics, data_filename = _load_and_process()
    filtered = metrics
    if cinsiyet:
        filtered = filtered[filtered["CİNSİYET"] == cinsiyet]
    if anagrup:
        filtered = filtered[filtered["ANAGRUP"] == anagrup]
    if alt_kategori:
        filtered = filtered[filtered["Alt Kategori"] == alt_kategori]

    pool = _worstseller_pool(filtered)
    expected_codes = [x.strip() for x in expected.split(",") if x.strip()]
    test_limit = min(limit, len(expected_codes)) if expected_codes else limit

    candidate_rules = {
        "st_asc": (["sell_through_pct"], [True]),
        "st_asc_dss_desc": (["sell_through_pct", "DSS Miktar"], [True, False]),
        "st_asc_dss_asc": (["sell_through_pct", "DSS Miktar"], [True, True]),
        "st_asc_satis_asc": (["sell_through_pct", "Satış Miktarı"], [True, True]),
        "st_asc_satis_desc": (["sell_through_pct", "Satış Miktarı"], [True, False]),
        "st_asc_cover_desc": (["sell_through_pct", "periyot_cover_19"], [True, False]),
        "st_asc_cover_asc": (["sell_through_pct", "periyot_cover_19"], [True, True]),
        "st_asc_satis_asc_dss_desc": (["sell_through_pct", "Satış Miktarı", "DSS Miktar"], [True, True, False]),
        "st_asc_satis_asc_dss_asc": (["sell_through_pct", "Satış Miktarı", "DSS Miktar"], [True, True, True]),
        "st_asc_satis_desc_dss_desc": (["sell_through_pct", "Satış Miktarı", "DSS Miktar"], [True, False, False]),
        "st_asc_cover_desc_dss_desc": (["sell_through_pct", "periyot_cover_19", "DSS Miktar"], [True, False, False]),
        "st_asc_cover_desc_dss_asc": (["sell_through_pct", "periyot_cover_19", "DSS Miktar"], [True, False, True]),
        "st_asc_dss_desc_satis_asc": (["sell_through_pct", "DSS Miktar", "Satış Miktarı"], [True, False, True]),
        "st_asc_dss_asc_satis_asc": (["sell_through_pct", "DSS Miktar", "Satış Miktarı"], [True, True, True]),
        "st_asc_stok_kodu_asc": (["sell_through_pct", "Stok Kodu"], [True, True]),
        "st_asc_stok_kodu_desc": (["sell_through_pct", "Stok Kodu"], [True, False]),
        "st_asc_marka_asc_dss_desc": (["sell_through_pct", "Marka Açıklama", "DSS Miktar"], [True, True, False]),
        "st_asc_marka_desc_dss_desc": (["sell_through_pct", "Marka Açıklama", "DSS Miktar"], [True, False, False]),
        "cover_desc": (["periyot_cover_19"], [False]),
        "cover_desc_dss_desc": (["periyot_cover_19", "DSS Miktar"], [False, False]),
    }

    matches = []
    expected_top = expected_codes[:test_limit]
    for name, (cols, asc) in candidate_rules.items():
        ranked = pool.sort_values(
            cols,
            ascending=asc,
            na_position="last",
            kind="mergesort",
        ).head(test_limit)
        actual = ranked["Stok Kodu"].astype(str).tolist()
        exact_positions = sum(
            1 for i, code in enumerate(expected_top) if i < len(actual) and actual[i] == code
        )
        overlap = len(set(expected_top).intersection(actual))
        first_mismatch = None
        for i, code in enumerate(expected_top):
            actual_code = actual[i] if i < len(actual) else None
            if actual_code != code:
                first_mismatch = {
                    "rank": i + 1,
                    "expected": code,
                    "actual": actual_code,
                }
                break
        matches.append(
            {
                "rule": name,
                "sort_columns": cols,
                "ascending": asc,
                "exact_positions": exact_positions,
                "overlap": overlap,
                "score": exact_positions * 100 + overlap,
                "first_mismatch": first_mismatch,
                "actual_codes": actual,
                "items": [_row_to_product(row) for _, row in ranked.iterrows()],
            }
        )

    matches.sort(key=lambda x: (x["score"], x["exact_positions"], x["overlap"]), reverse=True)

    return {
        "data_file": data_filename,
        "pool_count": len(pool),
        "expected_codes": expected_top,
        "filters_applied": {
            "cinsiyet": cinsiyet,
            "anagrup": anagrup,
            "alt_kategori": alt_kategori,
        },
        "best_match": matches[0] if matches else None,
        "matches": matches,
    }


def _score_worstseller_rules(pool: pd.DataFrame, expected_codes: list[str], limit: int) -> list[dict]:
    test_limit = min(limit, len(expected_codes)) if expected_codes else limit
    expected_top = expected_codes[:test_limit]
    candidate_rules = {
        "st_asc": (["sell_through_pct"], [True]),
        "st_asc_dss_desc": (["sell_through_pct", "DSS Miktar"], [True, False]),
        "st_asc_dss_asc": (["sell_through_pct", "DSS Miktar"], [True, True]),
        "st_asc_satis_asc": (["sell_through_pct", "Satış Miktarı"], [True, True]),
        "st_asc_satis_desc": (["sell_through_pct", "Satış Miktarı"], [True, False]),
        "st_asc_cover_desc": (["sell_through_pct", "periyot_cover_19"], [True, False]),
        "st_asc_cover_asc": (["sell_through_pct", "periyot_cover_19"], [True, True]),
        "st_asc_satis_asc_dss_desc": (["sell_through_pct", "Satış Miktarı", "DSS Miktar"], [True, True, False]),
        "st_asc_satis_asc_dss_asc": (["sell_through_pct", "Satış Miktarı", "DSS Miktar"], [True, True, True]),
        "st_asc_satis_desc_dss_desc": (["sell_through_pct", "Satış Miktarı", "DSS Miktar"], [True, False, False]),
        "st_asc_cover_desc_dss_desc": (["sell_through_pct", "periyot_cover_19", "DSS Miktar"], [True, False, False]),
        "st_asc_cover_desc_dss_asc": (["sell_through_pct", "periyot_cover_19", "DSS Miktar"], [True, False, True]),
        "st_asc_dss_desc_satis_asc": (["sell_through_pct", "DSS Miktar", "Satış Miktarı"], [True, False, True]),
        "st_asc_dss_asc_satis_asc": (["sell_through_pct", "DSS Miktar", "Satış Miktarı"], [True, True, True]),
        "st_asc_stok_kodu_asc": (["sell_through_pct", "Stok Kodu"], [True, True]),
        "st_asc_stok_kodu_desc": (["sell_through_pct", "Stok Kodu"], [True, False]),
        "st_asc_marka_asc_dss_desc": (["sell_through_pct", "Marka Açıklama", "DSS Miktar"], [True, True, False]),
        "st_asc_marka_desc_dss_desc": (["sell_through_pct", "Marka Açıklama", "DSS Miktar"], [True, False, False]),
        "cover_desc": (["periyot_cover_19"], [False]),
        "cover_desc_dss_desc": (["periyot_cover_19", "DSS Miktar"], [False, False]),
    }

    matches = []
    for name, (cols, asc) in candidate_rules.items():
        ranked = pool.sort_values(
            cols,
            ascending=asc,
            na_position="last",
            kind="mergesort",
        ).head(test_limit)
        actual = ranked["Stok Kodu"].astype(str).tolist()
        exact_positions = sum(
            1 for i, code in enumerate(expected_top) if i < len(actual) and actual[i] == code
        )
        overlap = len(set(expected_top).intersection(actual))
        first_mismatch = None
        for i, code in enumerate(expected_top):
            actual_code = actual[i] if i < len(actual) else None
            if actual_code != code:
                first_mismatch = {
                    "rank": i + 1,
                    "expected": code,
                    "actual": actual_code,
                }
                break
        matches.append(
            {
                "rule": name,
                "sort_columns": cols,
                "ascending": asc,
                "exact_positions": exact_positions,
                "overlap": overlap,
                "score": exact_positions * 100 + overlap,
                "first_mismatch": first_mismatch,
                "actual_codes": actual,
                "items": [_row_to_product(row) for _, row in ranked.iterrows()],
            }
        )
    matches.sort(key=lambda x: (x["score"], x["exact_positions"], x["overlap"]), reverse=True)
    return matches


@app.get("/api/debug/worstseller-auto-match")
def debug_worstseller_auto_match(
    sheet: str | None = Query(None, description="Boşsa adı Worstseller içeren sayfa aranır"),
    cinsiyet: str | None = Query(None),
    anagrup: str | None = Query(None),
    alt_kategori: str | None = Query(None),
    limit: int = Query(10, ge=1, le=50),
):
    path = resolve_data_path()
    if path.suffix.lower() not in (".xlsx", ".xlsm"):
        raise HTTPException(status_code=400, detail="Auto-match için Excel dosyası gerekir.")

    xls = pd.ExcelFile(path, engine="openpyxl")
    selected_sheet = sheet
    if not selected_sheet:
        candidates = [s for s in xls.sheet_names if "worst" in s.lower()]
        selected_sheet = candidates[0] if candidates else xls.sheet_names[0]

    sample = pd.read_excel(path, sheet_name=selected_sheet, engine="openpyxl")
    sample = _normalize_columns(sample)
    if "Stok Kodu" not in sample.columns:
        raise HTTPException(
            status_code=400,
            detail=f"{selected_sheet} sayfasında Stok Kodu kolonu bulunamadı.",
        )

    expected_codes = (
        sample["Stok Kodu"]
        .dropna()
        .astype(str)
        .map(str.strip)
        .loc[lambda s: s != ""]
        .head(limit)
        .tolist()
    )

    metrics, data_filename = _load_and_process()
    filtered = metrics
    if cinsiyet:
        filtered = filtered[filtered["CİNSİYET"] == cinsiyet]
    if anagrup:
        filtered = filtered[filtered["ANAGRUP"] == anagrup]
    if alt_kategori:
        filtered = filtered[filtered["Alt Kategori"] == alt_kategori]

    pool = _worstseller_pool(filtered)
    matches = _score_worstseller_rules(pool, expected_codes, limit)

    return {
        "data_file": data_filename,
        "sheet_names": xls.sheet_names,
        "selected_sheet": selected_sheet,
        "pool_count": len(pool),
        "expected_codes": expected_codes,
        "filters_applied": {
            "cinsiyet": cinsiyet,
            "anagrup": anagrup,
            "alt_kategori": alt_kategori,
        },
        "best_match": matches[0] if matches else None,
        "matches": matches,
    }


@app.get("/api/matrix")
def matrix(
    view: str = Query("best", description="best veya worst"),
    per_cell: int = Query(5, ge=1, le=10, description="Her hücrede kaç ürün"),
):
    """
    Cinsiyet × ANAGRUP matrisi: Her hücrede o segmentin top ürünleri.
    Talep #1 için: Cinsiyet bazlı her ana grubun haftalık bestseller/worstseller analizi.
    """
    metrics, data_filename = _load_and_process()

    cinsiyet_vals = sorted(metrics["CİNSİYET"].dropna().unique().tolist(), key=str)
    anagrup_vals = sorted(metrics["ANAGRUP"].dropna().unique().tolist(), key=str)

    cells = {}
    for c in cinsiyet_vals:
        for a in anagrup_vals:
            cell = metrics[(metrics["CİNSİYET"] == c) & (metrics["ANAGRUP"] == a)]
            if view == "best":
                top = cell.sort_values(
                    ["sell_through_pct", "Satış Miktarı", "Satış Tutarı", "brut_kar"],
                    ascending=[False, False, False, False],
                    na_position="last",
                    kind="mergesort",
                ).head(per_cell)
            else:
                ws_pool = _worstseller_pool(cell)
                top = _sort_worstsellers(ws_pool).head(per_cell)
            key = f"{c} × {a}"
            cells[key] = {
                "cinsiyet": c,
                "anagrup": a,
                "count": len(cell),
                "items": [_row_to_product(top.iloc[i]) for i in range(len(top))],
            }

    return {
        "cinsiyet_options": cinsiyet_vals,
        "anagrup_options": anagrup_vals,
        "cells": cells,
        "view": view,
        "meta": {"data_file": data_filename},
    }


@app.get("/api/analytics/summary")
def analytics_summary(
    sezon: str | None = Query(None),
    anagrup: str | None = Query(None),
    cinsiyet: str | None = Query(None),
):
    """Marka, kategori, sezon ve ST% dağılımı bazlı analitik özet."""
    metrics, data_filename = _load_and_process()
    metrics = _apply_category_mappings(metrics)

    sezon_col = _get_sezon_col(metrics)
    sezon_opts = sorted(metrics[sezon_col].dropna().astype(str).unique().tolist()) if sezon_col else []
    anagrup_opts = sorted(metrics["_ana_kategori"].dropna().unique().tolist())
    cinsiyet_opts = sorted(metrics["CİNSİYET"].dropna().unique().tolist(), key=str)

    df = metrics.copy()
    if sezon and sezon_col:
        df = df[df[sezon_col] == sezon]
    if anagrup:
        df = df[df["_ana_kategori"] == anagrup]
    if cinsiyet:
        df = df[df["CİNSİYET"] == cinsiyet]

    def _safe_float(v):
        try:
            f = float(v)
            return round(f, 2) if not (f != f) else 0.0
        except Exception:
            return 0.0

    def agg_group(grouped_df, key_col):
        rows = []
        for name, grp in grouped_df.groupby(key_col, dropna=True):
            qty = float(grp["Satış Miktarı"].fillna(0).sum())
            dss = float(grp["DSS Miktar"].fillna(0).sum())
            ciro = float(grp["Satış Tutarı"].fillna(0).sum())
            kar = float(grp["brut_kar"].fillna(0).sum()) if "brut_kar" in grp.columns else 0.0
            st = round(qty / (qty + dss) * 100, 1) if (qty + dss) > 0 else 0.0
            rows.append({"label": str(name), "satis": round(qty), "ciro": round(ciro, 2),
                         "kar": round(kar, 2), "dss": round(dss), "st_pct": st,
                         "sku_count": int(grp["Stok Kodu"].nunique())})
        return sorted(rows, key=lambda x: x["satis"], reverse=True)

    by_brand = agg_group(df, "Marka Açıklama")[:15]

    kat_col = "_grup" if "_grup" in df.columns else "Alt Kategori"
    by_kategori = agg_group(df, kat_col)[:15]

    by_anagrup = agg_group(df, "_ana_kategori") if "_ana_kategori" in df.columns else []

    if sezon_col and sezon_col in df.columns:
        by_sezon = agg_group(df, sezon_col)
    else:
        by_sezon = []

    by_cinsiyet = agg_group(df, "CİNSİYET") if "CİNSİYET" in df.columns else []

    # ST% dağılımı
    st_col = df["sell_through_pct"].fillna(0) if "sell_through_pct" in df.columns else pd.Series([0.0] * len(df))
    buckets = [
        {"label": "%0 (Satışsız)", "min": 0, "max": 0},
        {"label": "%1–25", "min": 1, "max": 25},
        {"label": "%26–50", "min": 26, "max": 50},
        {"label": "%51–75", "min": 51, "max": 75},
        {"label": "%76–100", "min": 76, "max": 100},
    ]
    st_dist = []
    for b in buckets:
        if b["min"] == 0 and b["max"] == 0:
            cnt = int((st_col == 0).sum())
        else:
            cnt = int(((st_col >= b["min"]) & (st_col <= b["max"])).sum())
        st_dist.append({"label": b["label"], "count": cnt})

    total_qty = float(df["Satış Miktarı"].fillna(0).sum())
    total_dss = float(df["DSS Miktar"].fillna(0).sum())
    total_ciro = float(df["Satış Tutarı"].fillna(0).sum())
    total_kar = float(df["brut_kar"].fillna(0).sum()) if "brut_kar" in df.columns else 0.0
    avg_st = round(total_qty / (total_qty + total_dss) * 100, 1) if (total_qty + total_dss) > 0 else 0.0

    # Yıldız ürünler: sell_through_pct DESC, satış miktarı > 0
    star_df = df[df["Satış Miktarı"].fillna(0) > 0].copy()
    if "sell_through_pct" in star_df.columns:
        star_df = star_df.sort_values(
            ["sell_through_pct", "Satış Miktarı", "brut_kar"] if "brut_kar" in star_df.columns else ["sell_through_pct", "Satış Miktarı"],
            ascending=[False, False, False] if "brut_kar" in star_df.columns else [False, False],
            na_position="last",
        ).head(8)
    else:
        star_df = star_df.sort_values("Satış Miktarı", ascending=False).head(8)

    def _build_product_card(row):
        sku = str(row.get("Stok Kodu", ""))
        gmroi_val = row.get("gmroi") if "gmroi" in row.index else None
        try:
            gmroi_num = round(float(gmroi_val), 2) if gmroi_val is not None and not pd.isna(gmroi_val) else None
        except (ValueError, TypeError):
            gmroi_num = None
        return {
            "stok_kodu": sku,
            "stok_aciklama": str(row.get("Stok Açıklama", row.get("Stok Aciklama", ""))),
            "marka": str(row.get("Marka Açıklama", row.get("Marka Aciklama", ""))),
            "renk": str(row.get("Renk Açıklama", row.get("Renk Aciklama", ""))),
            "satis": int(row.get("Satış Miktarı", 0) or 0),
            "ciro": round(float(row.get("Satış Tutarı", 0) or 0), 2),
            "kar": round(float(row.get("brut_kar", 0) or 0), 2) if "brut_kar" in row.index else 0.0,
            "st_pct": round(float(row.get("sell_through_pct", 0) or 0), 1),
            "dss": int(row.get("DSS Miktar", 0) or 0),
            "gmroi": gmroi_num,
            "gorsel_link": f"/api/image/{sku}" if sku else "",
        }

    top_products = [_build_product_card(row) for _, row in star_df.iterrows()]

    # Risk Ürünleri (worst sellers): DSS > 0 ve düşük sell-through
    worst_df = df[df["DSS Miktar"].fillna(0) > 0].copy()
    if "E-TİCARET RENK" in worst_df.columns:
        worst_df["E-TİCARET RENK"] = worst_df["E-TİCARET RENK"].fillna("").astype(str).str.strip()
        worst_df = worst_df[(worst_df["E-TİCARET RENK"] != "") & (worst_df["E-TİCARET RENK"] != "999 - BOŞ")]
    if "sell_through_pct" in worst_df.columns:
        worst_df = worst_df.sort_values(
            ["sell_through_pct", "DSS Miktar"],
            ascending=[True, False],
            na_position="last",
        ).head(8)
    else:
        worst_df = worst_df.sort_values("DSS Miktar", ascending=False).head(8)

    worst_products = [_build_product_card(row) for _, row in worst_df.iterrows()]

    return {
        "summary": {
            "total_satis": round(total_qty),
            "total_ciro": round(total_ciro, 2),
            "total_kar": round(total_kar, 2),
            "avg_st_pct": avg_st,
            "toplam_sku": int(df["Stok Kodu"].nunique()),
            "toplam_dss": round(total_dss),
        },
        "top_products": top_products,
        "worst_products": worst_products,
        "by_brand": by_brand,
        "by_kategori": by_kategori,
        "by_anagrup": by_anagrup,
        "by_sezon": by_sezon,
        "by_cinsiyet": by_cinsiyet,
        "st_distribution": st_dist,
        "filter_options": {
            "sezon_options": sezon_opts,
            "anagrup_options": anagrup_opts,
            "cinsiyet_options": cinsiyet_opts,
        },
        "meta": {"data_file": data_filename},
    }


@app.get("/api/health")
def health():
    return {"ok": True}


@app.get("/api/debug/raw-data")
def debug_raw_data():
    """Ham verinin durumunu kontrol et."""
    try:
        path = resolve_data_path()
        df = pd.read_excel(path)
        return {
            "path": str(path),
            "columns": df.columns.tolist(),
            "row_count": len(df),
            "first_5_rows": df.head(5).to_dict(orient="records"),
            "summary_markers_present": {m: m in df.columns for m in SUMMARY_MARKERS}
        }
    except Exception as e:
        return {"error": str(e)}


@app.post("/api/admin/precache-images")
def admin_precache_images(
    size: int = Query(300, ge=64, le=800),
    limit: int | None = Query(None, ge=1, description="Opsiyonel SKU sayısı limiti"),
    timeout: float = Query(2.0, ge=0.2, le=10.0, description="İndirme zaman aşımı (s)"),
):
    """
    Tüm benzersiz SKU'lar için Sporthink CDN'den görselleri indirip local'e kaydeder.
    Frontend bundan sonra /static/images/SKU.jpeg olarak kullanır.
    """
    metrics, data_filename = _load_and_process()
    skus = (
        metrics["Stok Kodu"].dropna().astype(str).map(str.strip).loc[lambda s: s != ""].unique().tolist()
    )
    downloaded = 0
    skipped = 0
    failures = []
    raw_links = _load_raw_image_links()
    placeholders: list[str] = []
    # Build simple info map for queries
    info_cols = [c for c in ["Stok Kodu", "Stok Kodu Açıklama", "Marka Açıklama", "Alt Kategori"] if c in metrics.columns]
    sku_info: dict[str, tuple[str, str, str]] = {}
    if info_cols:
        for _, r in metrics[info_cols].dropna(how="all").iterrows():
            sku = str(r.get("Stok Kodu", "") or "").strip().upper()
            if not sku:
                continue
            brand = str(r.get("Marka Açıklama", "") or "").strip()
            desc = str(r.get("Stok Kodu Açıklama", "") or "").strip()
            altk = str(r.get("Alt Kategori", "") or "").strip()
            sku_info[sku] = (brand, desc, altk)
    if limit is not None:
        skus = skus[: int(limit)]
    dl_timeout = float(timeout)
    for sk in skus:
        key = sk.strip().upper()
        # Skip if already cached
        if _local_image_url_for_sku(key):
            skipped += 1
            continue
        # 1) Try raw link from data file if available
        raw_url = raw_links.get(key)
        if raw_url:
            low = raw_url.lower()
            ext = ".jpeg" if low.endswith(".jpeg") else ".jpg" if low.endswith(".jpg") else ".png" if low.endswith(".png") else ".jpeg"
            dest = IMAGES_DIR / f"{key}{ext}"
            if _download_image(raw_url, dest, timeout=dl_timeout):
                downloaded += 1
                continue
        # Try candidates directly (some CDNs block HEAD)
        tried_ok = False
        for url in _cdn_candidates_for_sku(key, size=size):
            low = url.lower()
            ext = ".jpeg" if low.endswith(".jpeg") else ".jpg" if low.endswith(".jpg") else ".png" if low.endswith(".png") else ".jpeg"
            dest = IMAGES_DIR / f"{key}{ext}"
            if _download_image(url, dest, timeout=dl_timeout):
                downloaded += 1
                tried_ok = True
                break
        # 3) Parse Sporthink search page
        if not tried_ok:
            found = _discover_cdn_via_search(key, size=size, timeout=dl_timeout)
            if found:
                low = found.lower()
                ext = ".jpeg" if low.endswith(".jpeg") else ".jpg" if low.endswith(".jpg") else ".png" if low.endswith(".png") else ".jpeg"
                dest = IMAGES_DIR / f"{key}{ext}"
                if _download_image(found, dest, timeout=dl_timeout):
                    downloaded += 1
                    tried_ok = True
        # 4) As a broader fallback, web image search (Bing)
        if not tried_ok:
            b, d, a = sku_info.get(key, ("", "", ""))
            q = (f"{b} {d}".strip() or f"{key} {a}".strip() or key)
            found2 = _discover_image_via_bing(q, timeout=dl_timeout)
            if found2:
                low = found2.lower()
                ext = ".jpeg" if low.endswith(".jpeg") else ".jpg" if low.endswith(".jpg") else ".png" if low.endswith(".png") else ".jpeg"
                dest = IMAGES_DIR / f"{key}{ext}"
                if _download_image(found2, dest, timeout=dl_timeout):
                    downloaded += 1
                    tried_ok = True
        if not tried_ok:
            # ensure placeholder so that UI never lacks an image
            ph = _ensure_placeholder_svg()
            dest = IMAGES_DIR / f"{key}.svg"
            try:
                with open(ph, "r", encoding="utf-8") as rf, open(dest, "w", encoding="utf-8") as wf:
                    wf.write(rf.read())
                placeholders.append(key)
            except Exception:
                failures.append(key)

    return {
        "data_file": data_filename,
        "sku_total": len(skus),
        "downloaded": downloaded,
        "skipped_existing": skipped,
        "placeholders": placeholders[:50],
        "failures": failures[:50],
        "images_dir": str(IMAGES_DIR),
    }


@app.get("/api/debug/product/{stok_kodu}")
def debug_product(stok_kodu: str):
    # ... (existing code)
    """Belirli bir stok kodunun ham veri → KPI zincirini göster."""
    metrics, data_filename = _load_and_process()
    row = metrics[metrics["Stok Kodu"] == stok_kodu]
    if row.empty:
        return {"error": f"{stok_kodu} bulunamadı"}
    r = row.iloc[0]
    def _safe_float(x):
        try:
            if pd.isna(x):
                return None
        except Exception:
            pass
        try:
            return float(x)
        except Exception:
            return None
    qty = _safe_float(r.get("Satış Miktarı")) or 0.0
    ciro = _safe_float(r.get("Satış Tutarı")) or 0.0
    net_ciro = ciro / (1.0 + VAT_DEFAULT)
    smm = _safe_float(r.get("smm"))
    unit_cost = _safe_float(r.get("birim_alis_fiyati"))
    unit_rev = (ciro / qty) if qty else None
    return {
        "stok_kodu": stok_kodu,
        "data_file": data_filename,
        "raw_inputs": {
            "Satış Miktarı": qty,
            "Satış Tutarı (Ciro)": ciro,
            "Net Ciro (KDV hariç)": net_ciro,
            "DSS Miktar": _safe_float(r.get("DSS Miktar")),
            "birim_alis_fiyati": unit_cost,
            "psf": _safe_float(r.get("psf")),
            "alis_qty_sum": _safe_float(r.get("alis_qty_sum")),
            "alis_total_sum": _safe_float(r.get("alis_total_sum")),
        },
        "computed": {
            "smm": smm,
            "initial_ciro": _safe_float(r.get("initial_ciro")),
            "brut_kar": _safe_float(r.get("brut_kar")),
            "toplam_kar": _safe_float(r.get("toplam_kar")),
            "mu": _safe_float(r.get("mu")),
            "sell_through_pct": _safe_float(r.get("sell_through_pct")),
            "periyot_cover_19": _safe_float(r.get("periyot_cover_19")),
            "unit_rev": unit_rev,
            "unit_cost": unit_cost,
        },
        "verify": {
            "kar_manual_brut": (ciro - smm) if (smm is not None) else None,
            "kar_manual_net": (net_ciro - smm) if (smm is not None) else None,
            "mu_manual": (ciro / smm) if (smm not in (None, 0)) else None,
        },
    }
