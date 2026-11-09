# MedAccess: Medicine Uses & Side Effects Matching Model

Part of the **MedAccess Intelligent Clinical Intelligence Suite**.

---

## 1. Overview & Objective

The **Medicine Uses & Side Effects Matcher** matches user-entered medicine names (brand names, partial queries, common misspellings, or names missing dosage-form words) against the curated clinical repository (`data/Medicine_Details.csv`).

When a patient or pharmacist searches for a medicine, the model:
1. Normalizes the user query.
2. Projects it into a character n-gram TF-IDF vector space.
3. Computes cosine similarity against 11,498 indexed pharmaceutical formulations.
4. Categorizes match confidence into three discrete decision gates:
   - **`HIGH_CONFIDENCE_MATCH`** ($\ge 0.75$)
   - **`MEDIUM_CONFIDENCE_MATCH`** ($0.50 \le \text{score} < 0.75$)
   - **`UNCERTAIN_OR_NO_MATCH`** ($< 0.50$)
5. Safely retrieves parsed clinical **Uses** (indications) and merged **Side Effects** without hallucination or destructive splitting.

---

## 2. Model Architecture & Hyperparameters

- **Algorithm**: Character N-Gram TF-IDF Vectorizer + Cosine Similarity Ranking
- **Analyzer**: `char_wb` (character n-grams bounded by word edges)
- **N-gram Range**: `(2, 5)` (bigrams through 5-grams)
- **Sublinear TF**: `True` (dampens repetitive token dominance with $1 + \log(\text{tf})$)
- **Vocabulary Size**: `51,685` distinct character n-grams
- **Corpus Dimension**: `11,498` unique consolidated medicine formulations
- **Matrix Sparsity**: $0.119\%$ non-zero entries (highly efficient compressed sparse row representation)

---

## 3. Data Preprocessing Strategy

1. **Exact Duplicate Removal**:
   - `84` duplicate rows were identified and removed, preserving canonical records.
2. **Handling 299 Duplicate Medicine Names**:
   - Grouped by unique medicine name to eliminate duplicate indices.
   - For records exhibiting multiple side effect observations (e.g., `Tegliptin Tablet`), descriptions were merged preserving unique symptoms without losing multi-word clinical phrases.
3. **Uses Text Cleansing**:
   - Raw scraping artifacts that concatenated multiple indications (e.g. `Treatment of Acid refluxTreatment of Peptic ulcer disease`) were cleanly segmented on regex boundaries: `(?=(?:Treatment of|Treatment and prevention of|Prevention of)\b)`.
   - Generates both discrete lists (`uses_list`) and formatted strings (`uses_cleaned`).
4. **Side Effects Preservation**:
   - Multi-word phrases like `High blood pressure`, `Common cold`, `Urinary incontinence`, and `Abdominal pain` were preserved in their natural clinical format rather than broken by naive single-letter splitting.

---

## 4. Evaluation Benchmark Results

Evaluated on a rigorous test harness across 408 multi-category test cases with zero data leakage:

| Evaluation Category | Top-1 Accuracy | Top-3 Accuracy | Mean Similarity | Behavior Notes |
| :--- | :---: | :---: | :---: | :--- |
| **Exact Name (Random Casing)** | **100.0%** | **100.0%** | `1.0000` | Full case insensitivity |
| **Short / Brand-Only Names** | **100.0%** | **100.0%** | `0.8632` | Matches brand root accurately |
| **Missing Dosage Form Words** | **98.9%** | **100.0%** | `0.9451` | Absorbs missing "Tablet/Syrup/Injection" |
| **Spelling Typos & Swaps** | **95.0%** | **97.0%** | `0.7954` | Robust to 1-2 character edits |
| **Unknown / Synthetic Names** | **N/A** | **N/A** | `0.2951` | Max: `0.4700` (reliably below threshold) |

---

## 5. Directory Structure & Artifacts

```
ml_models/medicine_side_effects_model/
├── config.json                             # Model metadata and threshold parameters
├── side_effects_model_bundle.pkl          # Self-contained runtime bundle (6.4 MB)
├── vectorizer.pkl                          # Fitted TfidfVectorizer (347 KB)
├── tfidf_matrix.pkl                        # Precomputed CSR sparse matrix (5.0 MB)
├── medicine_records.pkl                    # Serialized list of 11,498 cleaned records (987 KB)
├── medicine_records.json                   # Human-readable JSON repository (11 MB)
├── predict_medicine.py                     # Standalone CLI and Python API
├── medicine_side_effects_training.ipynb    # Reproducible training Jupyter notebook
└── README.md                               # Model documentation
```

---

## 6. Usage & Python API

### CLI Execution
```powershell
# High confidence match
python ml_models/medicine_side_effects_model/predict_medicine.py "Azithral 500 tab"

# Typo tolerance
python ml_models/medicine_side_effects_model/predict_medicine.py "Augmetin"

# Unknown medicine rejection
python ml_models/medicine_side_effects_model/predict_medicine.py "NonExistentMedFake"
```

### Python Service Integration
```python
from ml_models.medicine_side_effects_model.predict_medicine import predict_medicine_details

# Execute prediction
result = predict_medicine_details("Augmentin 625", top_k=3)

print("Match Status:", result["match_status"])
print("Similarity Score:", result["similarity_score"])

if result["best_match"]:
    print("Medicine:", result["best_match"]["medicine_name"])
    print("Composition:", result["best_match"]["composition"])
    print("Uses:", result["best_match"]["uses_cleaned"])
    print("Side Effects:", result["best_match"]["side_effects_merged"])
```

---

## 7. Regulatory & Clinical Safety Notice

- **No Diagnostic Claims**: The similarity score represents **orthographic and lexical text alignment**, **not** a clinical diagnostic probability or efficacy metric.
- **Uncertainty Protection**: Queries with scores below $0.50$ are flagged as `UNCERTAIN_OR_NO_MATCH` to prevent false medical associations.
- **Informational Reference**: All retrieved indications and side-effect profiles reflect the historical `Medicine_Details.csv` dataset and are strictly intended for patient reference and pharmacy informational screening.

