import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import brieflow_interview as bi  # noqa: E402

def test_species_id_from_ontology_term_id():
    assert bi._compute_species_id({"experiment": {"organism_ontology_term_id": "NCBITaxon:9606"}}) == {"species_id": "9606"}
    assert bi._compute_species_id({"experiment": {"organism_ontology_term_id": "NCBITaxon:10090"}}) == {"species_id": "10090"}

def test_species_id_name_fallback_when_no_ontology_id():
    assert bi._compute_species_id({"experiment": {"organism": "Mus musculus"}}) == {"species_id": "10090"}

def test_species_id_missing_returns_none():
    assert bi._compute_species_id({"experiment": {}}) is None
    assert bi._compute_species_id({}) is None

def test_chemistry_normalizes_space_form():
    assert bi._compute_chemistry({"iss": {"chemistry": "four color"}}) == {"chemistry": "four_color"}
    assert bi._compute_chemistry({"iss": {"chemistry": "Four Color"}}) == {"chemistry": "four_color"}
    assert bi._compute_chemistry({"iss": {"chemistry": "combinatorial"}}) == {"chemistry": "combinatorial"}
    assert bi._compute_chemistry({"iss": {}}) is None

def test_registered():
    assert "species_id" in bi._COMPUTED_DERIVATIONS
    assert "chemistry" in bi._COMPUTED_DERIVATIONS
