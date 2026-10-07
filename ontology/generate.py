"""
Generate the BPX application ontology (``bpx.ttl``) from the ``bpx`` pydantic schema
and the hand-curated table in ``mapping.yaml``.

Usage, from the repository root::

    python ontology/generate.py                # write ontology/bpx.ttl
    python ontology/generate.py --check        # fail if bpx.ttl is out of date
    python ontology/generate.py --mint         # mint IRIs for new class rows, then write
    python ontology/generate.py --verify-terms DIR [DIR ...]
                                               # check every external term against
                                               # local copies of the domain ontologies

Generation needs only the runtime dependencies of ``bpx`` (pydantic, PyYAML).
``--verify-terms`` also needs rdflib.
"""

from __future__ import annotations

import argparse
import inspect
import re
import sys
import types
import typing
import uuid
from dataclasses import dataclass, field
from pathlib import Path

import yaml
from pydantic import BaseModel

import bpx
from bpx.interpolated_table import InterpolatedTable
from bpx.schema import BPX, Particle

HERE = Path(__file__).resolve().parent
MAPPING_PATH = HERE / "mapping.yaml"
TTL_PATH = HERE / "bpx.ttl"

# Kept from the earlier BattMoTeam/application-bpx draft, which w3id.org has rules for.
NAMESPACE = "https://w3id.org/emmo/application/bpx#"
ONTOLOGY_IRI = "https://w3id.org/emmo/application/bpx"
LOCAL_NAME_PREFIX = "bpx_"
LOCAL_NAME_RE = re.compile(LOCAL_NAME_PREFIX + r"[0-9a-f]{8}(_[0-9a-f]{4}){3}_[0-9a-f]{12}")

PREFIXES = {
    "": NAMESPACE,
    "owl": "http://www.w3.org/2002/07/owl#",
    "rdf": "http://www.w3.org/1999/02/22-rdf-syntax-ns#",
    "rdfs": "http://www.w3.org/2000/01/rdf-schema#",
    "xsd": "http://www.w3.org/2001/XMLSchema#",
    "skos": "http://www.w3.org/2004/02/skos/core#",
    "dcterms": "http://purl.org/dc/terms/",
    "vann": "http://purl.org/vocab/vann/",
    "emmo": "https://w3id.org/emmo#",
    "echem": "https://w3id.org/emmo/domain/electrochemistry#",
    "battery": "https://w3id.org/emmo/domain/battery#",
}

# Blocks in the order they appear in bpx.ttl, with the prefix their class labels get.
# State blocks get no prefix: their keys already say what they are.
BLOCK_PREFIX = {
    "Cell": "Cell",
    "Electrolyte": "Electrolyte",
    "Electrode": "Electrode",
    "Particle": "Particle",
    "Separator": "Separator",
    "Initial conditions": "",
    "Thermal environment": "",
    "Degradation": "",
}
ELECTRODE_SECTIONS = ("Negative electrode", "Positive electrode")
# Pydantic models that are values (a table of numbers), not blocks of the file.
VALUE_MODELS = (InterpolatedTable,)
BASES = ("specific", "bridged", "gap")
CLASS_KEYS = {
    "iri",
    "slots",
    "label",
    "parent",
    "about",
    "basis",
    "gap",
    "ask",
    "related",
    "comment",
    "elucidation",
    "alt_labels",
    "note",
}
UNIT_RE = re.compile(r"\s*\[([^\]]+)\]\s*$")
NO_DESCRIPTION = "The BPX schema gives this field no description; the elucidation was written for this ontology."
SPLIT_DESCRIPTION = (
    "The BPX schema describes each of these keys separately; the elucidation was written for this ontology."
)


class MappingError(Exception):
    """The schema, the mapping table and the generated ontology disagree."""


# --------------------------------------------------------------------------------------
# Reading the schema
# --------------------------------------------------------------------------------------


@dataclass
class Slot:
    """One BPX key in one block, with every place it can sit in a file."""

    block: str
    key: str
    paths: set[str] = field(default_factory=set)
    descriptions: set[str] = field(default_factory=set)


@dataclass
class Schema:
    slots: dict[str, Slot]
    open_blocks: set[str]


def _is_block_model(annotation: object) -> bool:
    return (
        isinstance(annotation, type) and issubclass(annotation, BaseModel) and not issubclass(annotation, VALUE_MODELS)
    )


def _is_dict_of_blocks(annotation: object) -> bool:
    return typing.get_origin(annotation) is dict and _is_block_model(typing.get_args(annotation)[1])


def _block_of(path: tuple[str, ...], definer: type) -> str:
    top = path[0]
    if top == "Parameterisation":
        section = path[1]
        if section in ELECTRODE_SECTIONS:
            return "Particle" if issubclass(definer, Particle) else "Electrode"
        return section
    if top == "State":
        return path[1]
    return top


def _defining_model(model: type[BaseModel], name: str) -> type:
    for cls in model.__mro__:
        if name in inspect.get_annotations(cls):
            return cls
    msg = f"cannot find where {model.__name__}.{name} is declared"
    raise MappingError(msg)


class _SchemaWalker:
    def __init__(self) -> None:
        self.schema = Schema(slots={}, open_blocks=set())

    def walk(self, model: type[BaseModel], path: tuple[str, ...]) -> None:
        if model.model_config.get("extra") == "allow":
            self.schema.open_blocks.add("/".join(path))
            return
        for name, info in model.model_fields.items():
            self.visit(info.annotation, model, name, (*path, info.alias or name))

    def visit(self, annotation: object, model: type[BaseModel], name: str, path: tuple[str, ...]) -> None:
        if typing.get_origin(annotation) in (typing.Union, types.UnionType):
            members = [a for a in typing.get_args(annotation) if a is not type(None)]
            if any(_is_block_model(a) or _is_dict_of_blocks(a) for a in members):
                for member in members:
                    self.visit(member, model, name, path)
                return
        if _is_dict_of_blocks(annotation):
            self.walk(typing.get_args(annotation)[1], (*path, "*"))
        elif _is_block_model(annotation):
            self.walk(annotation, path)
        else:
            self.add_leaf(annotation, model, name, path)

    def add_leaf(self, annotation: object, model: type[BaseModel], name: str, path: tuple[str, ...]) -> None:
        block = _block_of(path, _defining_model(model, name))
        slot = self.schema.slots.setdefault(f"{block}/{path[-1]}", Slot(block=block, key=path[-1]))
        slot.paths.add("/".join(path))
        # A value that may also be given per active material, as a dict keyed by name.
        members = typing.get_args(annotation) or (annotation,)
        if any(typing.get_origin(m) is dict for m in members):
            slot.paths.add("/".join((*path, "*")))
        description = model.model_fields[name].description
        if description:
            slot.descriptions.add(description)


def read_schema() -> Schema:
    """Walk the BPX pydantic models and return every key, grouped by block."""
    walker = _SchemaWalker()
    walker.walk(BPX, ())
    return walker.schema


# --------------------------------------------------------------------------------------
# Checking the mapping table
# --------------------------------------------------------------------------------------


def load_mapping(path: Path = MAPPING_PATH) -> dict:
    with path.open(encoding="utf-8") as f:
        return yaml.safe_load(f)


def coverage_problems(schema: Schema, mapping: dict) -> list[str]:
    """List every disagreement between the fields of the schema and the mapping rows."""
    problems = []
    claimed: dict[str, str] = {}
    rows = [(e.get("label") or e["slots"][0], e["slots"]) for e in mapping.get("classes", [])]
    rows += [("unclassed", e["slots"]) for e in mapping.get("unclassed", [])]
    for owner, slots in rows:
        for slot_id in slots:
            if slot_id in claimed:
                problems.append(f"slot {slot_id!r} is mapped twice ({claimed[slot_id]}, {owner})")
            claimed[slot_id] = owner
    problems += [f"schema field {s!r} has no row in mapping.yaml" for s in sorted(set(schema.slots) - set(claimed))]
    stale = sorted(set(claimed) - set(schema.slots))
    problems += [f"mapping.yaml names {s!r}, which is not a field of the schema" for s in stale]
    listed_open = {e["path"] for e in mapping.get("open", [])}
    problems += [f"open block {p!r} is not listed under 'open'" for p in sorted(schema.open_blocks - listed_open)]
    problems += [f"'open' lists {p!r}, which is not an open block" for p in sorted(listed_open - schema.open_blocks)]
    return problems


def _row_problems(row: dict, terms: dict, labels: set[str]) -> list[str]:
    name = row["label"]
    problems = [f"{name}: unknown key {k!r}" for k in sorted(set(row) - CLASS_KEYS)]
    if row.get("basis") not in BASES:
        problems.append(f"{name}: basis must be one of {', '.join(BASES)}")
    if row.get("basis") == "gap" and not row.get("gap"):
        problems.append(f"{name}: a gap row must say which domain term is missing")
    if row.get("basis") == "bridged" and not row.get("about"):
        problems.append(f"{name}: a bridged row needs 'about'")
    problems += [
        f"{name}: {ref!r} is not listed under 'terms'"
        for ref in [row.get("parent"), *row.get("related", [])]
        if ref not in terms
    ]
    about = row.get("about")
    if about and about not in terms and about not in labels:
        problems.append(f"{name}: about {about!r} is neither a term nor a class of this ontology")
    if not LOCAL_NAME_RE.fullmatch(str(row.get("iri"))):
        problems.append(f"{name}: no minted IRI (run python ontology/generate.py --mint)")
    return problems


# --------------------------------------------------------------------------------------
# Building the ontology
# --------------------------------------------------------------------------------------


def unit_of(key: str) -> str | None:
    match = UNIT_RE.search(key)
    return match.group(1) if match else None


def auto_label(block: str, key: str) -> str:
    """CamelCase label from a BPX key: unit stripped, prefixed with the block name."""
    words = re.split(r"[^A-Za-z0-9]+", UNIT_RE.sub("", key))
    camel = "".join(w[0].upper() + w[1:] for w in words if w)
    prefix = BLOCK_PREFIX[block]
    return camel if camel.startswith(prefix) else prefix + camel


@dataclass
class OntologyClass:
    iri: str
    label: str
    block: str
    parent: str
    about: str | None
    elucidation: str
    comments: list[str]
    paths: list[str]
    unit: str | None
    related: list[str]
    alt_labels: list[str]
    summary: str


def _elucidation(row: dict, slots: list[Slot]) -> tuple[str, str | None]:
    """Return the elucidation and, when it is not the schema's own text, a comment saying so."""
    descriptions = set().union(*(s.descriptions for s in slots))
    if row.get("elucidation"):
        if len(descriptions) == 1:
            msg = f"{row['label']}: the schema describes this field; drop 'elucidation' from mapping.yaml"
            raise MappingError(msg)
        return " ".join(row["elucidation"].split()), SPLIT_DESCRIPTION if descriptions else NO_DESCRIPTION
    if len(descriptions) != 1:
        msg = f"{row['label']}: the schema gives {len(descriptions)} descriptions; write one under 'elucidation'"
        raise MappingError(msg)
    return descriptions.pop(), None


def _make_class(row: dict, slots: list[Slot], terms: dict, iri_by_label: dict[str, str]) -> OntologyClass:
    units = {unit_of(s.key) for s in slots}
    if len(units) != 1 or len({s.block for s in slots}) != 1:
        msg = f"{row['label']}: the slots of one class must share a block and a unit"
        raise MappingError(msg)
    about = row.get("about")
    elucidation, provenance = _elucidation(row, slots)
    comments = [" ".join(row["comment"].split())] if row.get("comment") else []
    if provenance:
        comments.append(provenance)
    return OntologyClass(
        iri=NAMESPACE + row["iri"],
        label=row["label"],
        block=slots[0].block,
        parent=terms[row["parent"]]["iri"],
        about=(terms[about]["iri"] if about in terms else iri_by_label[about]) if about else None,
        elucidation=elucidation,
        comments=comments,
        paths=sorted(set().union(*(s.paths for s in slots))),
        unit=units.pop(),
        related=sorted(terms[r]["iri"] for r in row.get("related", [])),
        alt_labels=sorted(row.get("alt_labels", [])),
        summary=row["parent"] + (f" and (isPropertyFor some {about})" if about else ""),
    )


def build_classes(schema: Schema, mapping: dict) -> list[OntologyClass]:
    """Check the mapping against the schema and turn each class row into a class."""
    problems = coverage_problems(schema, mapping)
    if problems:
        raise MappingError("\n".join(problems))
    rows = mapping["classes"]
    blocks = {schema.slots[s].block for row in rows for s in row["slots"]}
    problems = [
        f"block {b!r} has classes but is not in BLOCK_PREFIX in generate.py" for b in blocks - set(BLOCK_PREFIX)
    ]
    if problems:
        raise MappingError("\n".join(problems))
    for row in rows:
        first = schema.slots[row["slots"][0]]
        row.setdefault("label", auto_label(first.block, first.key))
    labels = [row["label"] for row in rows]
    iris = [row.get("iri") for row in rows if row.get("iri")]
    problems = [f"label {x!r} is used twice" for x in sorted({x for x in labels if labels.count(x) > 1})]
    problems += [f"IRI {x} is used twice" for x in sorted({x for x in iris if iris.count(x) > 1})]
    for row in rows:
        problems += _row_problems(row, mapping["terms"], set(labels))
    used = {mapping["ontology"]["bridge_property"], mapping["ontology"]["elucidation_property"]}
    for row in rows:
        used.update([row.get("parent"), row.get("about"), *row.get("related", [])])
    problems += [f"term {name!r} is not used by any row" for name in sorted(set(mapping["terms"]) - used)]
    if problems:
        raise MappingError("\n".join(problems))

    iri_by_label = {row["label"]: NAMESPACE + row["iri"] for row in rows}
    classes = [
        _make_class(row, [schema.slots[s] for s in row["slots"]], mapping["terms"], iri_by_label) for row in rows
    ]
    order = list(BLOCK_PREFIX)
    return sorted(classes, key=lambda c: (order.index(c.block), c.label))


def _lit(text: str, lang: str | None = "en") -> str:
    escaped = text.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
    return f'"{escaped}"' + (f"@{lang}" if lang else "")


def _ref(iri: str) -> str:
    for prefix, ns in PREFIXES.items():
        if iri.startswith(ns) and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", iri[len(ns) :]):
            return f"{prefix}:{iri[len(ns) :]}"
    return f"<{iri}>"


def _statements(subject: str, pairs: list[tuple[str, list[str]]]) -> str:
    body = " ;\n".join(f"    {pred} " + " ,\n        ".join(objs) for pred, objs in pairs if objs)
    return f"{subject}\n{body} .\n"


@dataclass
class _Context:
    elucidation: str
    bridge: str
    bpx_path: str
    bpx_unit: str


def _header(mapping: dict, version: str) -> str:
    onto = mapping["ontology"]
    pairs = [
        ("rdf:type", ["owl:Ontology"]),
        ("owl:versionIRI", [f"<{ONTOLOGY_IRI}/{version}/bpx>"]),
        ("owl:versionInfo", [_lit(version, None)]),
        ("owl:imports", [f"<{i}>" for i in onto["imports"]]),
        ("dcterms:title", [_lit(onto["title"])]),
        ("dcterms:abstract", [_lit(" ".join(onto["abstract"].split()))]),
        ("dcterms:license", [f"<{onto['license']}>"]),
        ("dcterms:source", [f"<{onto['source']}>"]),
        ("vann:preferredNamespacePrefix", [_lit("bpx", None)]),
        ("vann:preferredNamespaceUri", [_lit(NAMESPACE, None)]),
        ("rdfs:comment", [_lit(f"Generated by ontology/generate.py from bpx {version}. Edit mapping.yaml instead.")]),
    ]
    return _statements(f"<{ONTOLOGY_IRI}>", pairs)


def _annotation_properties(mapping: dict, ctx: _Context) -> str:
    out = []
    for label, prop in sorted(mapping["annotation_properties"].items()):
        pairs = [
            ("rdf:type", ["owl:AnnotationProperty"]),
            ("skos:prefLabel", [_lit(label)]),
            (_ref(ctx.elucidation), [_lit(" ".join(prop["elucidation"].split()))]),
        ]
        out.append(f"###  {NAMESPACE}{prop['iri']}  {label}\n" + _statements(_ref(NAMESPACE + prop["iri"]), pairs))
    return "\n".join(out)


def _external_declarations(ctx: _Context) -> str:
    """Declare the external properties used here, so the file is valid OWL on its own."""
    skos = PREFIXES["skos"]
    declarations = [(ctx.elucidation, "owl:AnnotationProperty"), (ctx.bridge, "owl:ObjectProperty")]
    declarations += [(skos + p, "owl:AnnotationProperty") for p in ("altLabel", "prefLabel", "related")]
    return "".join(f"{_ref(iri)} rdf:type {kind} .\n" for iri, kind in sorted(declarations))


def _class_block(cls: OntologyClass, ctx: _Context) -> str:
    parents = [_ref(cls.parent)]
    if cls.about:
        parents.append(
            "[ rdf:type owl:Restriction ;\n"
            f"          owl:onProperty {_ref(ctx.bridge)} ;\n"
            f"          owl:someValuesFrom {_ref(cls.about)}\n"
            "        ]",
        )
    pairs = [
        ("rdf:type", ["owl:Class"]),
        ("rdfs:subClassOf", parents),
        ("skos:prefLabel", [_lit(cls.label)]),
        ("skos:altLabel", [_lit(a) for a in cls.alt_labels]),
        (_ref(ctx.elucidation), [_lit(cls.elucidation)]),
        (_ref(ctx.bpx_path), [_lit(p, None) for p in cls.paths]),
        (_ref(ctx.bpx_unit), [_lit(cls.unit, None)] if cls.unit else []),
        ("skos:related", [_ref(r) for r in cls.related]),
        ("rdfs:comment", [_lit(c) for c in cls.comments]),
    ]
    heading = f"###  {cls.iri}  {cls.label}\n#    subClassOf {cls.summary}\n"
    return heading + _statements(_ref(cls.iri), pairs)


def build_turtle(schema: Schema, mapping: dict, version: str = bpx.__version__) -> str:
    """Return the ontology as Turtle text, deterministic for a given schema and mapping."""
    classes = build_classes(schema, mapping)
    terms, onto, props = mapping["terms"], mapping["ontology"], mapping["annotation_properties"]
    ctx = _Context(
        elucidation=terms[onto["elucidation_property"]]["iri"],
        bridge=terms[onto["bridge_property"]]["iri"],
        bpx_path=NAMESPACE + props["bpxPath"]["iri"],
        bpx_unit=NAMESPACE + props["bpxUnit"]["iri"],
    )
    rule = "#" * 88
    parts = [
        "".join(f"@prefix {p}: <{ns}> .\n" for p, ns in PREFIXES.items()),
        _header(mapping, version),
        f"{rule}\n#    Annotation properties\n{rule}\n",
        _annotation_properties(mapping, ctx),
        f"{rule}\n#    External properties used in this file\n{rule}\n",
        _external_declarations(ctx),
    ]
    for block in BLOCK_PREFIX:
        parts.append(f"{rule}\n#    Classes: {block}\n{rule}\n")
        parts += [_class_block(c, ctx) for c in classes if c.block == block]
    return "\n".join(parts)


# --------------------------------------------------------------------------------------
# Minting IRIs
# --------------------------------------------------------------------------------------


def mint(path: Path = MAPPING_PATH) -> int:
    """Give every class row without an IRI a new one, editing mapping.yaml in place."""
    text = path.read_text(encoding="utf-8")
    start = re.search(r"^classes:\s*$", text, flags=re.MULTILINE)
    if start is None:
        msg = "mapping.yaml has no 'classes:' section"
        raise MappingError(msg)
    end = re.compile(r"^\S", flags=re.MULTILINE).search(text, start.end())
    stop = end.start() if end else len(text)
    count = 0

    def add_iri(match: re.Match) -> str:
        nonlocal count
        count += 1
        local = LOCAL_NAME_PREFIX + str(uuid.uuid4()).replace("-", "_")
        return f"{match.group(1)}- iri: {local}\n{match.group(1)}  slots:"

    section = re.sub(r"^( *)- slots:", add_iri, text[start.end() : stop], flags=re.MULTILINE)
    path.write_text(text[: start.end()] + section + text[stop:], encoding="utf-8", newline="\n")
    return count


# --------------------------------------------------------------------------------------
# Checking external terms against local copies of the domain ontologies
# --------------------------------------------------------------------------------------


def _load_modules(sources: list[Path]) -> tuple[dict, list[str]]:
    """Parse every Turtle file under ``sources``, keyed by its owl:versionIRI."""
    from rdflib import OWL, Graph  # noqa: PLC0415 - rdflib is a development dependency

    modules, unreadable = {}, []
    for source in sources:
        for ttl in sorted(source.rglob("*.ttl")) if source.is_dir() else [source]:
            graph = Graph()
            try:
                graph.parse(ttl, format="turtle")
            except SyntaxError as error:
                unreadable.append(f"{ttl}: {str(error).splitlines()[0]}")
                continue
            for version_iri in graph.objects(None, OWL.versionIRI):
                modules[str(version_iri)] = graph
    return modules, unreadable


def _import_closure(modules: dict, roots: list[str]) -> set[str]:
    from rdflib import OWL  # noqa: PLC0415

    closure: set[str] = set()
    todo = list(roots)
    while todo:
        module = todo.pop()
        if module in modules and module not in closure:
            closure.add(module)
            todo += [str(o) for o in modules[module].objects(None, OWL.imports)]
    return closure


def _term_problem(name: str, term: dict, modules: dict, closure: set[str]) -> str | None:
    from rdflib import OWL, RDF, URIRef  # noqa: PLC0415
    from rdflib.namespace import SKOS  # noqa: PLC0415

    iri, module = URIRef(term["iri"]), term["module"]
    graph = modules.get(module)
    if graph is None or module not in closure:
        return f"{name}: module {module} is not in the import closure"
    if not any((iri, RDF.type, kind) in graph for kind in (OWL.Class, OWL.ObjectProperty, OWL.AnnotationProperty)):
        return f"{name}: {iri} is not declared in {module}"
    if name not in {str(label) for label in graph.objects(iri, SKOS.prefLabel)}:
        return f"{name}: the prefLabel of {iri} in {module} is not {name!r}"
    if any(str(flag).lower() == "true" for flag in graph.objects(iri, OWL.deprecated)):
        return f"{name}: {iri} is deprecated"
    return None


def verify_terms(mapping: dict, sources: list[Path]) -> list[str]:
    """
    Check every entry under 'terms' against the ontology files found in ``sources``: it
    must be declared, with that prefLabel and not deprecated, in the module the table
    names, and the module must be in the import closure of our owl:imports. Also check
    that no label of this ontology is already a prefLabel in that closure. Call after
    build_turtle(), which fills in the generated labels.
    """
    from rdflib.namespace import SKOS  # noqa: PLC0415

    modules, unreadable = _load_modules(sources)
    for message in unreadable:
        sys.stderr.write(f"skipped, does not parse: {message}\n")
    roots = mapping["ontology"]["imports"]
    closure = _import_closure(modules, roots)
    problems = [f"imported module {m} is not among the sources" for m in roots if m not in modules]
    for name, term in sorted(mapping["terms"].items()):
        problem = _term_problem(name, term, modules, closure)
        if problem:
            problems.append(problem)
    labels = {row["label"] for row in mapping["classes"]} | set(mapping["annotation_properties"])
    for module in sorted(closure):
        for subject, label in modules[module].subject_objects(SKOS.prefLabel):
            if str(label) in labels and not str(subject).startswith(NAMESPACE):
                problems.append(f"label {label} is already used by {subject} in {module}")
    return problems


# --------------------------------------------------------------------------------------
# Command line
# --------------------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--check", action="store_true", help="fail if bpx.ttl is not up to date")
    parser.add_argument("--mint", action="store_true", help="mint IRIs for class rows that have none")
    parser.add_argument("--verify-terms", nargs="+", type=Path, metavar="DIR", help="ontology files to check against")
    args = parser.parse_args(argv)

    if args.mint:
        sys.stdout.write(f"minted {mint()} IRI(s) in {MAPPING_PATH.name}\n")
    mapping = load_mapping()
    try:
        turtle = build_turtle(read_schema(), mapping)
    except MappingError as error:
        sys.stderr.write(f"{error}\n")
        return 1
    if args.verify_terms:
        problems = verify_terms(mapping, args.verify_terms)
        sys.stdout.write("".join(f"{p}\n" for p in problems) or f"all {len(mapping['terms'])} terms verified\n")
        return 1 if problems else 0
    if args.check:
        current = TTL_PATH.read_text(encoding="utf-8").replace("\r\n", "\n") if TTL_PATH.exists() else ""
        if current != turtle:
            sys.stderr.write("ontology/bpx.ttl is out of date; run python ontology/generate.py\n")
            return 1
        return 0
    TTL_PATH.write_text(turtle, encoding="utf-8", newline="\n")
    sys.stdout.write(f"wrote {TTL_PATH.relative_to(HERE.parent)}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
