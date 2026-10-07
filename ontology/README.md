# BPX application ontology

`bpx.ttl` is an OWL ontology with one class per BPX parameter. Each class sits under a term from the EMMO domain ontologies for electrochemistry and batteries, so a value typed with a BPX class can be found and compared alongside data described with those ontologies. The file is generated from the pydantic schema in `bpx/schema.py` and the curated table in `mapping.yaml`, and it carries the version of the `bpx` package it was generated from.

## Files

- `generate.py` walks the schema, joins it with the mapping table and writes `bpx.ttl`.
- `mapping.yaml` is the hand-curated part: one row per class with its IRI, parent, bridge filler and notes, plus the rows for schema fields that get no class and the list of external terms.
- `bpx.ttl` is the generated ontology. It is committed, and the tests fail if it differs from a fresh generation.

## Regenerating

From the repository root, with the package installed (`pip install -e .[dev]`):

```
python ontology/generate.py              # rewrite bpx.ttl
python ontology/generate.py --check      # exit 1 if bpx.ttl is stale
python ontology/generate.py --mint       # new class rows: mint their IRIs, then rewrite
```

When the schema gains a field, `tests/test_ontology.py` fails until `mapping.yaml` has a row for it. Add the row without an `iri`, run with `--mint`, and commit the mapping and the regenerated `bpx.ttl` together. A row that names a field the schema no longer has also fails the tests. A version bump of `bpx` changes `owl:versionIRI`, so it needs a regeneration too.

To check the external terms, point the generator at checkouts of the released domain ontologies (the tags matching the `owl:imports`: domain-battery 0.20.2, domain-electrochemistry 0.37.2, EMMO 1.0.2):

```
python ontology/generate.py --verify-terms ../domain-battery ../domain-electrochemistry ../EMMO
```

It checks that every IRI under `terms` is declared, with that prefLabel and not deprecated, in the module the table names, that the module is in the import closure of `bpx.ttl`, and that none of our labels is already a prefLabel there. Setting `BPX_ONTOLOGY_SOURCES` (paths separated by `os.pathsep`) runs the same check inside the test suite. It needs rdflib, which is a dev dependency only.

## Modelling rules

1. One class per schema field, without polarity. BPX 1.1 uses one `Particle` model for both electrodes and for every phase of a blended electrode, so a particle field is one class (`ParticleDiffusivity`), never a negative and a positive variant. Which electrode or phase a value belongs to follows from where it sits in the file. The two State fields that carry polarity in the key (initial hysteresis state, LAM) are each one class covering both keys.
2. The same key in different blocks gives different classes: `Conductivity [S.m-1]` is `ElectrolyteConductivity` in the electrolyte and `ElectrodeConductivity` in an electrode.
3. IRIs are opaque (`bpx_<uuid4>`) with the readable name in `skos:prefLabel`, as in EMMO. Once minted, an IRI is stored in `mapping.yaml` and never changes.
4. The label is the block name plus the key in CamelCase, unit stripped (`Particle` + `Diffusivity activation energy [J.mol-1]` gives `ParticleDiffusivityActivationEnergy`). State classes take no block prefix. A row may override the label: acronyms are spelled out (`OCP`, `LLI`, `LAM`), and three labels take a `Bpx` prefix because the plain name is already a domain term (`BpxParticleRadius`, `BpxSeparatorThickness`, `BpxSeparatorPorosity`).
5. Each class has an `emmo:elucidation` taken verbatim from the schema description. Where the schema has none, the row supplies one and the class gets an `rdfs:comment` saying so. `bpxPath` gives every place the value can sit in a file, with `*` for a key the author chooses (a particle phase, a material, a validation experiment). `bpxUnit` gives the unit as written in the key.
6. Parents follow the depth rule: the domain ontologies hold base quantities with at most one facet, and pre-coordinated classes like these belong here, defined over domain terms. Where the parent already says what the quantity is of (`DeviceVolume`, `SeparatorPorosity`), it is used alone. Where it is generic, the class also gets a bridge restriction `isPropertyFor some X`, for example `ParticleDiffusivityActivationEnergy ⊑ ActivationEnergy ⊓ ∃isPropertyFor.ParticleDiffusivity`. EMMO 1.0.2 has no `isAbout`, so the bridge uses `isPropertyFor` from perspectives/semiotics, the inverse of the `hasProperty` relation the domain ontologies use to attach quantities to components.
7. The polarity-qualified compounds in domain-electrochemistry (`GuestDiffusivityInNegativeElectrodeActiveMaterial` and the like) are frozen upstream and never used as parents. They appear as `skos:related` so the two can be matched.
8. Where no adequate domain term exists, the parent is the nearest generic EMMO quantity and the row records the gap.

## Asks for the domain ontologies

Rows with `basis: gap` in `mapping.yaml`, as of BPX 1.1.1:

- Transport efficiency, for electrodes and the separator: the inverse MacMullin number, a ratio. It is not `BruggemanCoefficient`, which is the exponent. Proposed `TransportEfficiency` in domain-electrochemistry, next to `Tortuosity`.
- Number of electrode pairs connected in parallel: proposed `NumberOfElectrodePairs`, a sibling of `NumberOfCellsConnectedInParallel`. Now under `NumberOfEntities`.
- Loss of lithium inventory and loss of active material: degradation-mode quantities (fractions). domain-electrochemistry has capacity loss and fade, but not these. Now under `RatioQuantity`.
- OCP hysteresis decay constant and initial hysteresis state: quantities of OCP hysteresis models. Both are tied to a model family and may well stay here; a generic hysteresis-state quantity upstream would give them a better parent than `ISQDimensionlessQuantity`.

Rows with an `ask` have an adequate parent today but would move to a closer one: `GuestDiffusivity` and `EntropicChangeCoefficient` (BattINFO ontology-additions section 10a), and a way to name the lithiation or delithiation branch of a hysteretic OCP.

The heat transfer coefficient is not a gap: EMMO's `SurfaceCoefficientOfHeatTransfer` is the coefficient between a body and its surroundings. Initial state of charge is not one either: it is a `StateOfCharge`, and "initial" is a fact of the State block.

## Not covered

The `Header` fields are file metadata and the `Validation` series are measured data, so neither gets classes. `User-defined` is open by design. All three are listed in `mapping.yaml` so the coverage test knows about them.

## To do before a release

- Fill in `dcterms:creator` and `dcterms:contributor`. They are left out on purpose rather than guessed.
- The w3id.org rules for `https://w3id.org/emmo/application/bpx` currently redirect to `emmo-repo/application-bpx`, which does not exist. They need repointing at this repository before the IRIs resolve.
- emmocheck passes with the configuration domain-battery uses in CI (which skips `test_namespace` and `test_quantity_dimension`). With the dimension test on, four classes fail because their domain parents (`ChargeCarrierDiffusivityInElectrolyte`, `VolumetricSurfaceArea`, `TemperatureCoefficientOfTheOpenCircuitVoltage`, `ReactionRateConstant`) declare no measurement unit; that is for the domain ontologies to fix.
