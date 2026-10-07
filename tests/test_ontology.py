"""Checks that ontology/bpx.ttl and ontology/mapping.yaml stay in step with the schema."""

import copy
import importlib.util
import os
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _load_generator() -> types.ModuleType:
    spec = importlib.util.spec_from_file_location("bpx_ontology_generate", ROOT / "ontology" / "generate.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


gen = _load_generator()


@pytest.fixture
def schema() -> gen.Schema:
    return gen.read_schema()


@pytest.fixture
def mapping() -> dict:
    return gen.load_mapping()


def test_every_schema_field_has_a_mapping_row(schema: gen.Schema, mapping: dict) -> None:
    assert gen.coverage_problems(schema, mapping) == []


def test_a_new_schema_field_is_reported(schema: gen.Schema, mapping: dict) -> None:
    schema.slots["Cell/Made-up key [m]"] = gen.Slot(block="Cell", key="Made-up key [m]")
    assert gen.coverage_problems(schema, mapping) == [
        "schema field 'Cell/Made-up key [m]' has no row in mapping.yaml",
    ]


def test_a_stale_mapping_row_is_reported(schema: gen.Schema, mapping: dict) -> None:
    mapping["classes"][0]["slots"].append("Cell/Removed key [m]")
    assert gen.coverage_problems(schema, mapping) == [
        "mapping.yaml names 'Cell/Removed key [m]', which is not a field of the schema",
    ]


def test_mint_adds_iris_to_class_rows_only(tmp_path: Path) -> None:
    table = tmp_path / "mapping.yaml"
    table.write_text(
        "classes:\n"
        "  - iri: bpx_00000000_0000_4000_8000_000000000000\n"
        '    slots: ["Cell/Volume [m3]"]\n'
        '  - slots: ["Cell/Density [kg.m-3]"]\n'
        "unclassed:\n"
        '  - slots: ["Header/BPX"]\n',
        encoding="utf-8",
    )
    assert gen.mint(table) == 1
    rows = gen.load_mapping(table)
    assert rows["classes"][0]["iri"] == "bpx_00000000_0000_4000_8000_000000000000"
    assert gen.LOCAL_NAME_RE.fullmatch(rows["classes"][1]["iri"])
    assert "iri" not in rows["unclassed"][0]


def test_every_class_row_is_complete(schema: gen.Schema, mapping: dict) -> None:
    classes = gen.build_classes(schema, copy.deepcopy(mapping))
    assert len(classes) == len(mapping["classes"])


def test_committed_ttl_matches_a_fresh_generation(schema: gen.Schema, mapping: dict) -> None:
    committed = (ROOT / "ontology" / "bpx.ttl").read_text(encoding="utf-8").replace("\r\n", "\n")
    assert committed == gen.build_turtle(schema, mapping), "bpx.ttl is stale: run python ontology/generate.py"


def test_generated_ttl_parses_and_is_well_formed(mapping: dict) -> None:
    rdflib = pytest.importorskip("rdflib")
    owl, rdf, rdfs, skos = rdflib.OWL, rdflib.RDF, rdflib.RDFS, rdflib.namespace.SKOS

    graph = rdflib.Graph().parse(ROOT / "ontology" / "bpx.ttl", format="turtle")
    elucidation = rdflib.URIRef(mapping["terms"]["elucidation"]["iri"])
    bpx_path = rdflib.URIRef(gen.NAMESPACE + mapping["annotation_properties"]["bpxPath"]["iri"])
    classes = [c for c in graph.subjects(rdf.type, owl.Class) if str(c).startswith(gen.NAMESPACE)]
    assert len(classes) == len(mapping["classes"])

    labels = []
    for cls in classes:
        assert gen.LOCAL_NAME_RE.fullmatch(str(cls)[len(gen.NAMESPACE) :])
        pref_labels = list(graph.objects(cls, skos.prefLabel))
        assert len(pref_labels) == 1, cls
        labels.append(str(pref_labels[0]))
        assert len(list(graph.objects(cls, elucidation))) == 1, cls
        assert list(graph.objects(cls, bpx_path)), cls
    assert len(labels) == len(set(labels))

    # Every external IRI a class points at is in the term list that --verify-terms checks.
    external = set()
    for cls in classes:
        for parent in graph.objects(cls, rdfs.subClassOf):
            if isinstance(parent, rdflib.BNode):
                external.update(graph.objects(parent, owl.onProperty))
                external.update(graph.objects(parent, owl.someValuesFrom))
            else:
                external.add(parent)
        external.update(graph.objects(cls, skos.related))
    external = {str(iri) for iri in external if not str(iri).startswith(gen.NAMESPACE)}
    known = {term["iri"] for term in mapping["terms"].values()}
    assert external <= known, sorted(external - known)


@pytest.mark.skipif(
    not os.environ.get("BPX_ONTOLOGY_SOURCES"),
    reason="set BPX_ONTOLOGY_SOURCES to checkouts of the released domain ontologies",
)
def test_terms_exist_in_the_released_domain_ontologies(schema: gen.Schema, mapping: dict) -> None:
    pytest.importorskip("rdflib")
    gen.build_turtle(schema, mapping)
    sources = [Path(p) for p in os.environ["BPX_ONTOLOGY_SOURCES"].split(os.pathsep)]
    assert gen.verify_terms(mapping, sources) == []
