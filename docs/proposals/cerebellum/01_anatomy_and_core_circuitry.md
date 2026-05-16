# Cerebellum Research — Slice 1: Anatomy & Core Circuitry

*Subagent 1 of 8 in the brainctl cerebellum swarm.*

The cerebellum is anatomically stereotyped to a degree unmatched elsewhere: the same six-cell-type microcircuit is tiled across the whole structure, and variation across "functional zones" comes from *which inputs/outputs* a tile is wired to, not from the local algorithm.

---

## Nuclei + layers map (table)

| Element | Receives | Sends | Role |
|---|---|---|---|
| **Molecular layer** | Parallel fibers (PFs), CF ascending branches | — | PF→PC + CF→PC synapses; basket + stellate inhibitory interneurons |
| **Purkinje layer** | PFs (~150,000 per PC), one CF | GABAergic inhibition to DCN | **Sole output of cerebellar cortex** [1][2] |
| **Granule layer** (densest neuron pop. in the brain) | Mossy fibers (MFs) onto glomerular rosettes (3–7 MFs/granule cell) | Axons ascend, bifurcate into PFs running 4–6 mm along folial axis [1] | Massive fan-out / sparse recoding of MF input |
| **Golgi cells** | PFs, MFs | Inhibits granule cells | Gain control, sparsification |
| **Basket + stellate cells** | PFs | Inhibit PC soma/AIS or distal dendrites [1] | Feed-forward inhibition, sharpens PC timing |
| **Dentate nucleus** (lateral DCN) | PCs from cerebrocerebellum + MF/CF collaterals | Superior peduncle → red nucleus, **VL thalamus → motor + prefrontal cortex** [4] | Planning, initiation, cognitive output |
| **Interposed nuclei** (globose + emboliform) | PCs from spinocerebellum | Superior peduncle → red nucleus → rubrospinal tract [4] | Limb coordination, reaching |
| **Fastigial nucleus** (medial DCN) | PCs from vestibulocerebellum + vermis | Inferior peduncle → vestibular nuclei, reticular formation [4] | Balance, posture, eye movement |
| **Inferior peduncle** | Afferents from medulla (incl. inferior olive → CFs), spinal cord | Vestibular efferents | Mostly input + vestibular output [3] |
| **Middle peduncle** | Pontine nuclei (relaying cortex) → MFs | — | Cortico-ponto-cerebellar input — the cognitive pipe [3] |
| **Superior peduncle** | Spinocerebellar tract (afferent) | DCN efferents → thalamus, red nucleus | Main output route [3][4] |

The three **functional zones** map cleanly onto this: vestibulocerebellum (flocculonodular lobe → fastigial → balance/VOR), spinocerebellum (vermis + intermediate cortex → interposed → motor coordination), cerebrocerebellum (lateral hemispheres → dentate → predictive/cognitive — the part most relevant here) [3].

---

## Mossy vs climbing fiber inputs

Two distinct kinds of input carry different information, and the whole computational story turns on this asymmetry.

**Mossy fibers (MFs) — context / state.** Origins: pontine nuclei (relaying cerebral cortex), spinal cord, vestibular system, reticular formation [1][3]. Terminate on granule cells via "rosettes" (3–7 MFs per granule cell). Granule axons bifurcate into **parallel fibers** crossing dendritic trees of 200–1000 PCs [1]. Each PC receives **~150,000 PFs** — a huge sparsely-active feature vector. High firing rate, distributed, redundant. MFs say *"this is the current state."*

**Climbing fibers (CFs) — error / surprise / teaching signal.** Origin: inferior olivary nucleus. After developmental pruning, **one CF per Purkinje cell** [1][5]. Each CF wraps a PC's dendritic tree with hundreds of contacts, producing a "complex spike." ~1 Hz firing, nearly all-or-nothing (graded modulation in recent work, but still sparse and high-impact) [5][6]. Each CF contacts 1–10 PCs total. Rare, strong, "teacher." CFs say *"something happened you didn't predict."*

The two inputs are not interchangeable channels — they are categorically different signal *types*: a dense state vector vs. a sparse error pulse. This asymmetry is the architectural keystone.

**Purkinje cells → DCN: inhibition modulating a tonic output.** PCs are GABAergic. The DCN is tonically active; cortical output is a *modulated inhibitory shadow* shaping that tonic drive. MF/CF collaterals also excite the DCN directly, so the DCN integrates raw drive + cortically-shaped inhibition [4].

---

## Microzones

The cortex is organized into **longitudinal modules** and finer **microzones** [7][8]. A *module* = a parasagittal strip of PCs receiving common CF input from one olivary subregion, projecting to one DCN territory. A *microzone* is the cortical component: a few hundred micrometers wide, parasagittally elongated, PCs sharing CF receptive-field properties. Each microzone is an independent learner taught by its own olivary subpopulation — a **specialist forward-model module** for one transformation. The cerebellum is a federation of thousands of these.

Crucially, parallel fibers cross *orthogonally* to microzones — a single PF carries context into many microzones at once, but only those whose CFs fire update weights for that PF. The geometry implements *broadcast state + targeted error* [7].

---

## Marr-Albus-Ito learning rule

Canonical cerebellar learning theory, built in three steps [5][9]: **Marr (1969)** — CFs are a teaching signal; PF→PC synapses *potentiate* on co-activation. **Albus (1971)** — correction: since PCs are inhibitory, the synapse must *depress*. **Ito & Kano (1982)** — confirmed experimentally: **LTD at PF→PC synapses** when PF and CF inputs co-occur within a short time window [5][9].

**The rule, one line:**

> If a parallel fiber synapse onto a Purkinje cell is active *and* the climbing fiber to that PC fires within a short window, weaken that PF→PC synapse.

Implications: a **sparse, supervised, error-gated Hebbian rule**; updates fire only when the rare teacher fires. The teacher is broadcast at PC level, but only synapses *also active* (PF coincidence) are eligible — credit assignment via coincidence. It is **anti-Hebbian by design** (LTD not LTP) because output is inhibitory: depressing PF→PC removes DCN inhibition, *increasing* downstream output. Less PC firing = more behavioral output. 50+ years on this remains dominant, though LTP, intrinsic plasticity, and plasticity at MF→granule and PC→DCN are also documented [9][10].

---

## Cognitive cerebellum

For decades the cerebellum was treated as purely motor. Two threads broke this.

**Schmahmann's clinical work.** Patients with cerebellar lesions sparing motor cortex show cognitive deficits: executive dysfunction, working-memory impairment, language problems, visual-spatial deficits, dysregulated affect. Schmahmann formalized this as **Cerebellar Cognitive Affective Syndrome (CCAS)** / **Schmahmann's Syndrome** [11][12]. His framing, **"dysmetria of thought,"** argues the cerebellum applies the same correction to cognition that it applies to movement — regulating speed, consistency, and appropriateness of mental operations the way it regulates rate, rhythm, and force of motion [11].

**Universal Cerebellar Transform (UCT).** Schmahmann's larger claim: the cerebellum performs **one operation** — a generic forward-model / error-correction transform — and the specific function depends on *what circuit it is inserted into* [11][12]. Motor loop → coordination. Prefrontal loop → smooth cognition. Limbic loop → affect regulation.

**Forward-model framing.** Wolpert, Miall, and Kawato (1998) argued the cerebellum maintains **internal forward models** predicting sensory consequences of motor commands; later work extended this to language, social cognition, and abstract prediction [13][14][15]. The cerebro-cerebellum (dentate → VL thalamus → prefrontal/parietal cortex) is the substrate in non-motor domains [14]. Combined: **the cerebellum is a domain-general predictor** — it learns "given this context, what comes next?" and signals when reality diverges, regardless of whether the variable is limb position or sentence completion.

---

## Engineering-relevant takeaways

- **One algorithm, many instances.** Same 6-cell microcircuit tiled across the structure, parallelized over thousands of input/output pairings.
- **Two-channel input is fundamental.** Dense, high-rate *state* (mossy→granule→parallel) and sparse, low-rate, high-impact *error* (climbing). Conflating them loses the architecture.
- **Massive granule-layer fan-out.** Each PC sees ~150k PFs — a sparse high-dimensional recoding close to a kernel projection. Pattern separation happens here, before upstream learning.
- **Output is inhibitory, tonically gated.** PCs sculpt a continuously-firing DCN. The cerebellum doesn't trigger actions; it *shapes* a baseline output stream.
- **Sparse teacher, broadcast, coincidence-gated.** "PF active AND CF fires" assigns credit only to relevant synapses — supervised learning without explicit credit assignment.
- **Modular tiling = federation of specialists.** Each microzone is a narrow expert with one olivary error source. No central controller; outputs aggregate at the DCN.
- **Output polarity is anti-correlated.** PCs are inhibitory: suppressing PC activity = expressing the behavior. Polarity matters when porting the analogy.
- **Predictor, not actor.** Forward predictions and comparison-based error signals, not action selection. Action selection lives in basal ganglia + cortex.
- **Generic, not motor-specific.** UCT and forward-model literature: the same circuit operates on any domain it gets wired to. Substrate uniform; function determined by routing.

---

## 3 ideas to codify first

1. **The MF / CF input split is load-bearing.** Any cerebellum-inspired layer should explicitly separate a high-bandwidth *context/state* pathway from a sparse, gated *error/surprise* pathway. They are different KINDS of signals — and the learning rule depends entirely on their interaction.

2. **Marr-Albus-Ito coincidence is the minimum viable learning primitive.** "Weaken the connection from features active just before an error" — sparse, supervised, error-gated, with credit assignment via coincidence — is the canonical computation. Everything else is plumbing around this rule.

3. **Microzones = federation of narrow specialists, not one big model.** The intelligence is in tiling thousands of small identical learners across thousands of input/error pairings and aggregating at the DCN. A useful cerebellar analogue is many small predictors paired to specific error channels, not one large predictor with many inputs.

---

## Sources

1. **Microcircuit overview** — [Parallel Fiber, ScienceDirect](https://www.sciencedirect.com/topics/neuroscience/parallel-fiber); [Climbing fiber, Wikipedia](https://en.wikipedia.org/wiki/Climbing_fiber); [Mossy Fibers, U. Wisconsin Neuroanatomy](https://neuroanatomy.wisc.edu/cere/text/P4/mossy.htm).
2. **Cortical layers** — [Organization of the Cerebellum, NCBI Bookshelf (Purves)](https://www.ncbi.nlm.nih.gov/books/NBK11132/).
3. **Peduncles + functional zones** — [Cerebellum, Neuroscience Online (UT Houston)](https://nba.uth.tmc.edu/neuroscience/m/s3/chapter05.html); [Cerebellum afferent/efferent connections, Kenhub](https://www.kenhub.com/en/library/anatomy/afferent-and-efferent-pathways-of-the-cerebellum).
4. **Deep cerebellar nuclei** — [Projections from the Cerebellum, NCBI Bookshelf](https://www.ncbi.nlm.nih.gov/books/NBK11100/); [Deep cerebellar nuclei, Wikipedia](https://en.wikipedia.org/wiki/Deep_cerebellar_nuclei); [Dentate nucleus, Wikipedia](https://en.wikipedia.org/wiki/Dentate_nucleus).
5. **CFs as error signal** — [Climbing Fibers Provide Graded Error Signals, Frontiers Syst Neurosci 2019, PMC6749063](https://pmc.ncbi.nlm.nih.gov/articles/PMC6749063/).
6. **Graded vs all-or-nothing CFs** — [Beyond "all-or-nothing" climbing fibers, Frontiers Neural Circuits 2013](https://www.frontiersin.org/journals/neural-circuits/articles/10.3389/fncir.2013.00115/full).
7. **Microzones / modules (Apps & Hawkes consensus)** — [Cerebellar Modules as Operational Processing Units, PMC6132822](https://pmc.ncbi.nlm.nih.gov/articles/PMC6132822/); [Cerebellar cortical organization: a one-map hypothesis, Nature Rev Neurosci](https://www.nature.com/articles/nrn2698).
8. **Olivocerebellar axon organization** — [Branching patterns of olivocerebellar axons, Frontiers Neural Circuits 2013](https://www.frontiersin.org/articles/10.3389/fncir.2013.00003/full).
9. **Marr-Albus-Ito retrospective** — [50 Years Since the Marr, Ito, and Albus Models of the Cerebellum, Neuroscience 2020](https://www.ibroneuroscience.org/article/S0306-4522(20)30396-1/fulltext); [arXiv preprint](https://arxiv.org/pdf/2003.05647).
10. **Plasticity heterogeneity** — [Depressed by learning, PMC6550343](https://pmc.ncbi.nlm.nih.gov/articles/PMC6550343/).
11. **Dysmetria of thought / CCAS** — [Schmahmann 2010, PubMed 21227233](https://pubmed.ncbi.nlm.nih.gov/21227233/); [CCAS Task Force Paper, PMC6978293](https://pmc.ncbi.nlm.nih.gov/articles/PMC6978293/).
12. **CCAS scale** — [CCAS scale, Brain 2018](https://academic.oup.com/brain/article/141/1/248/4676034).
13. **Internal models (Wolpert, Miall, Kawato 1998)** — [Internal models in the cerebellum, TICS 1998 (Wolpert lab PDF)](https://wolpertlab.neuroscience.columbia.edu/sites/default/files/content/papers/WolMiaKaw98.pdf).
14. **Cerebro-cerebellum as forward model** — [The Cerebro-Cerebellum as a Locus of Forward Model, Frontiers Syst Neurosci 2020](https://www.frontiersin.org/journals/systems-neuroscience/articles/10.3389/fnsys.2020.00019/full).
15. **Predictions and errors** — [Cerebellum, Predictions and Errors, PMC6340992](https://pmc.ncbi.nlm.nih.gov/articles/PMC6340992/).
