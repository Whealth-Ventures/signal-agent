# Portfolio context — what each company does, its competitors, and what moves it

Knowledge base for the weekly **Sector Agent** (`src/sector_main.py`). It grounds
the impact ranker: for each portfolio company it names the direct competitors to
watch and the external forces (regulatory, sector, macro) that materially help or
hurt the business. The one-line company/sector/geo list the agent searches on
lives in `inputs/portfolio.xlsx`; this file is the deeper "so what" layer and is
appended to the impact-ranking prompt when present.

> Sourced from per-company research (2026-07). India traction figures from
> third-party aggregators (Tracxn/Inc42/CB Insights) are approximate. Names follow
> the firm's portfolio page; likely legal names noted where they differ.
> Maintainers: keep competitors and sensitivities current — they're what the agent
> reasons over.

---

### BeatO
- **Sector**: Digital diabetes management · **Geo**: India
- **What they do**: App-plus-smartphone-glucometer platform selling test strips, CGMs and subscription coaching (diet, doctor, GLP-1-based weight programs) to Indian diabetics and pre-diabetics — a device/consumables D2C model fused with digital chronic care.
- **Key competitors**: sugar.fit, Twin Health, Fitterfly, Wellthy Therapeutics
- **What materially moves them**: Rising Indian diabetes/pre-diabetes prevalence and cheaper CGMs expand the market; wide GLP-1 adoption (Mounjaro/Wegovy now in India) is both a tailwind (new paid weight/metabolic programs) and a risk if pharma or pure-play weight-loss players capture that revenue directly.

### Mylo
- **Sector**: Mother & baby (community + D2C) · **Geo**: India
- **What they do**: Began as a pregnancy/parenting tracker and mom community; now monetizes mainly through its own D2C brands (baby personal care, mother wellness, Ayurveda, daily essentials) for expecting and new mothers, with a health-services vertical on top.
- **Key competitors**: FirstCry, BabyChakra (Mosaic Wellness), The Moms Co, Mamaearth
- **What materially moves them**: Sensitive to D2C economics — digital ad costs, quick-commerce/marketplace shelf competition from FirstCry and Mamaearth, and the funding climate; India's urban birth rate and young-parent spending set the ceiling on the base.

### Good Health Company
- **Sector**: Full-stack digital wellness clinic (D2C) · **Geo**: India
- **What they do**: Hyderabad-based digital clinic (Mars by GHC for men, Saturn by GHC for women): free online consults plus prescribed treatment courses and D2C products across sexual health, hair fall, skin and weight loss — telehealth-plus-pharmacy for stigmatized conditions.
- **Key competitors**: Bold Care, Man Matters (Mosaic Wellness), Kindly Health, Allo Health
- **What materially moves them**: Tightening regulation on tele-prescribing of sexual-health and weight-loss drugs (and ad-platform limits on the category) is the main risk; the 2023 Mojocare fraud collapse dented sector trust/funding, while GLP-1 availability opens a new prescription weight-loss line.

### Elevate Now
- **Sector**: Medical (GLP-1) weight loss · **Geo**: India
- **What they do**: Doctor-led medical weight-loss program pairing prescription pharmacotherapy (GLP-1s, supplements) with habit coaching, nutrition and a non-surgical gastric-balloon option — positioned as clinician-supervised, not a diet app.
- **Key competitors**: sugar.fit, HealthifyMe, Fitterfly, BeatO (weight line)
- **What materially moves them**: The 2025 India launches of branded GLP-1s (Lilly Mounjaro, Novo Wegovy) are a direct demand tailwind, but drug pricing, supply/shortages, regulatory limits on GLP-1 prescribing for weight loss, and pharma/pharmacies going direct-to-consumer are the biggest swings.

### BabyMD  (rebranding to "Hoola Health", 2026 — match both)
- **Sector**: Pediatric primary care (clinics) · **Geo**: India
- **What they do**: Tech-enabled network of pediatric clinics — vaccinations, consultations, developmental assessments, therapy and 24x7 AI-assisted doctor access — for urban new-age parents, largely private-pay.
- **Key competitors**: Rainbow Children's Medicare, Cloudnine, Babynama
- **What materially moves them**: Urban private-pay demand, so disposable-income swings and any shift in OPD/pediatric insurance coverage matter; national immunization-schedule changes and hospital chains adding pediatric outpatient wings pressure core vaccination + primary-care volume.

### Nivaan Care
- **Sector**: Pain management clinics · **Geo**: India
- **What they do**: Single-specialty, multidisciplinary chronic-pain clinic chain (pain physicians + physiotherapists + care coordinators) delivering non-surgical and minimally invasive daycare interventional procedures across Delhi-NCR, Mumbai, Jaipur and Lucknow.
- **Key competitors**: QI Spine Clinic; broadly hospital orthopedic/spine departments and physiotherapy chains (few pure-play interventional-pain chains — largely category-defining)
- **What materially moves them**: Insurance/reimbursement recognition of interventional pain procedures as covered daycare treatment is the biggest lever; an aging population and rising chronic back/knee pain expand demand, while surgeons steering patients to surgery and unregulated clinics compete for the same patients.

### 2070Health
- **Sector**: Healthcare venture studio · **Geo**: India
- **What they do**: Healthcare-focused venture studio (arm of W Health Ventures) that generates, validates and builds healthcare companies in-house, staffing founding teams and seeding portfolio firms (e.g. Elevate Now, Nivaan, Reveal HealthTech, Everhope).
- **Key competitors**: n/a (positions as India's first healthcare venture studio; closest analog is Redesign Health in the US)
- **What materially moves them**: The health-tech funding climate and exit/IPO environment drive both its capital (W Health fund cycles / LP appetite) and its ability to spin out fundable companies; strong portfolio outcomes validate the model, a funding winter or a portfolio flop undercuts it.

### Everhope Oncology
- **Sector**: Oncology daycare centers · **Geo**: India
- **What they do**: Chain of dedicated cancer daycare/infusion centers delivering chemotherapy, targeted therapy, precision-oncology pathways, diagnostics and supportive care in a non-hospital, healing-led setting.
- **Key competitors**: HealthCare Global (HCG), Cytecare, American Oncology Institute; troubled peer Karkinos Healthcare
- **What materially moves them**: Insurance/reimbursement coverage for outpatient daycare chemotherapy and cancer-drug pricing/access are decisive for its transparency/EMI model; rising cancer incidence and its Narayana Health clinical partnership are tailwinds, while hospital oncology departments and regulatory tightening on standalone daycare oncology are risks.

### Stealth B2B Services Co.
- **Sector**: B2B services (stealth) · **Geo**: Global (India/US corridor)
- **What they do**: Stealth-stage B2B services provider across the India–US corridor; no verifiable public information on offering, model or customers as of mid-2026 — profile intentionally minimal.
- **Key competitors**: n/a (early/stealth)
- **What materially moves them**: Cannot be assessed without a defined product; broadly, India–US trade/tariff policy, IT/services outsourcing demand, and US visa/immigration rules would plausibly affect a cross-border B2B services play. *(Keyword news-matching won't work until a name/product is public.)*

### Wysa
- **Sector**: AI mental-health / digital therapeutics · **Geo**: Global (UK, India, US)
- **What they do**: Clinically-validated, AI-guided mental-health app delivering evidence-based CBT and coaching, sold D2C and to employers, health plans and health systems (e.g. NHS); expanded via acquisitions of April Health and **Kins** (PT).
- **Key competitors**: Woebot, Headspace, Spring Health, Lyra Health, Ieso/Limbic
- **What materially moves them**: FDA/NHS/regulatory posture on AI-driven mental-health tools and payer reimbursement for digital therapeutics drive adoption; safety scrutiny of AI chatbots and employer-benefit budget cycles are the main downside risks. **Linked to Kins (owns it) — news on one is relevant to the other.**

### Jasper Health
- **Sector**: Digital oncology / cancer navigation · **Geo**: US
- **What they do**: Personalized virtual 1-on-1 cancer support pairing patients with oncology-trained, ACS-certified guides plus a digital planning platform; sold to health plans and self-insured employers to improve adherence and lower total cost of care, with a Medicare-focused navigation offering.
- **Key competitors**: Thyme Care, Reimagine Care, AccessHope, OncoHealth
- **What materially moves them**: CMS reimbursement for cancer navigation / principal illness navigation and Health-Related Social Needs codes directly expand or contract its payer market; oncology drug/cost trends and health-plan value-based-care appetite also move demand.

### Kins
- **Sector**: Hybrid MSK / physical therapy · **Geo**: US
- **What they do**: Hybrid physical therapy — 45–55 min 1-on-1 sessions at home (in-person or virtual) by licensed PTs with between-visit digital engagement, accepting most insurance and Medicare. **Acquired by Wysa (Sept 2025); now operates under Wysa.**
- **Key competitors**: Luna, Hinge Health, Sword Health, IncludeHealth
- **What materially moves them**: Medicare/commercial PT reimbursement rates and telehealth-coverage rules drive unit economics; MSK point-solution consolidation (and its own integration into Wysa) plus employer/payer MSK spend shape the trajectory.

### Violet Health
- **Sector**: Health equity / clinician upskilling · **Geo**: US
- **What they do**: SaaS that measures clinicians' cultural competence via a proprietary framework, then delivers tailored upskilling pathways and credentialing so provider organizations, payers and digital-health companies can deliver identity-centered care for BIPOC, LGBTQ+ and other underserved patients.
- **Key competitors**: Quality Interactions, Included Health (equity/navigation); largely niche otherwise
- **What materially moves them**: Demand is tied to DEI/health-equity spending and mandates (NCQA health-equity accreditation, CMS equity measures) — a policy/political retreat from DEI or equity reporting shrinks buyer budgets; renewed equity mandates expand them.

### Reveal HealthTech
- **Sector**: Healthcare AI & engineering services · **Geo**: Global (US clients, India delivery)
- **What they do**: Specialized data/AI/engineering services firm (with productized tools like BioCanvas for trial recruitment and Prism AI for ops automation) building and deploying bespoke AI for US healthcare and life-sciences, blending clinical domain expertise with offshore engineering.
- **Key competitors**: CitiusTech, ThoughtWorks, Persistent Systems; boutique healthcare-AI shops
- **What materially moves them**: Rides enterprise healthcare AI/gen-AI adoption budgets — HIPAA/FDA rules on clinical AI, hospital IT spend cycles, and the build-vs-buy shift toward in-house AI drive demand; a pullback in healthcare AI spend or commoditization by hyperscaler/LLM tooling erodes it.

### Ryse Health
- **Sector**: Value-based diabetes care ("phygital") · **Geo**: US
- **What they do**: Hybrid in-person + virtual specialty clinics for patients with uncontrolled Type 2 diabetes, combining CGM hardware and a self-management app under value-based/risk contracts with payers (notably CareFirst BlueCross BlueShield), paid on outcomes like A1c reduction rather than volume.
- **Key competitors**: Virta Health, Omada Health, Vida Health, Cecelia Health
- **What materially moves them**: Payer appetite for value-based/risk contracts and CGM reimbursement coverage; GLP-1 drug economics cut both ways (raising diabetes-management urgency but offering a competing pharma-only fix); Medicaid/commercial coverage shifts affect the addressable population.

### Everbright Health
- **Sector**: Behavioral health enablement (interventional psychiatry) · **Geo**: US
- **What they do**: Tech-enabled services platform that lets existing mental-health practices launch advanced interventions (TMS, SPRAVATO/esketamine and similar) as new service lines — supplying clinical infrastructure, trained staff, AI-driven patient identification, and full prior-auth/billing/compliance management.
- **Key competitors**: Osmind, Greenbrook TMS, Salience Health / Mindful Health Solutions
- **What materially moves them**: Highly sensitive to payer reimbursement and prior-auth policy for TMS and SPRAVATO and to FDA label expansions; expanded coverage/indications grow the pipeline, reimbursement cuts or tighter prior-auth criteria threaten the economics it exists to unlock.
