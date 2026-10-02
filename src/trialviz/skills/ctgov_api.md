# Skill: querying the ClinicalTrials.gov API

## What the registry holds
About 600,000 registered studies. Each record describes a trial's design and administration:
conditions, interventions, phases, sponsors, sites, dates, enrollment, status. It does **not**
answer clinical questions: no efficacy, survival, safety rates, prices or approvals.

## Query parameters (the `Query` object)
| Field | API parameter | Searches | Notes |
|---|---|---|---|
| `query_cond` | `query.cond` | conditions, keywords | Expands disease synonyms ("breast cancer" also finds "Breast Neoplasms"). |
| `query_intr` | `query.intr` | intervention names, other names, descriptions | Also matches trials that only *mention* a drug, e.g. as a comparator. |
| `query_term` | `query.term` | the whole record | Broadest; use for concepts with no dedicated field. |
| `query_spons` | `query.spons` | lead sponsor and collaborators | |
| `query_locn` | `query.locn` | facility, city, state, country | |
| `filter_advanced` | `filter.advanced` | any field via `AREA[...]` | Most precise; see syntax below. |
| `filter_overall_status` | `filter.overallStatus` | overall status | List of enum values. |

All set fields are ANDed. Every text field accepts the expression syntax below.

## Expression syntax (Essie)
- Terms and phrases: `pembrolizumab`, `"non-small cell lung cancer"`
- Boolean: `A AND B`, `A OR B`, `NOT A`, parentheses for grouping
- Field-scoped: `AREA[InterventionName]"nivolumab"`. Name searches still expand synonyms, so
  `AREA[InterventionName]keytruda` finds trials listing pembrolizumab or MK-3475.
- Ranges: `AREA[StartDate]RANGE[2015-01-01,MAX]`, `AREA[EnrollmentCount]RANGE[100,MAX]`
- Missing values: `AREA[Phase]MISSING`, `NOT AREA[StartDate]MISSING`

## Useful fields
| Field | Meaning | Values |
|---|---|---|
| `InterventionName`, `InterventionOtherName` | listed interventions and their synonyms | free text |
| `InterventionType` | | DRUG, BIOLOGICAL, DEVICE, PROCEDURE, RADIATION, BEHAVIORAL, GENETIC, DIETARY_SUPPLEMENT, COMBINATION_PRODUCT, DIAGNOSTIC_TEST, OTHER |
| `Condition`, `Keyword` | conditions as the sponsor wrote them | free text |
| `ConditionMeshTerm`, `ConditionAncestorTerm` | NLM MeSH terms for the conditions, and their broader terms | MeSH headings |
| `InterventionMeshTerm` | NLM MeSH terms for the interventions | MeSH headings (chemical, not drug class) |
| `Phase` | a trial can list two (PHASE1 + PHASE2) | EARLY_PHASE1, PHASE1, PHASE2, PHASE3, PHASE4, NA |
| `OverallStatus` | | RECRUITING, NOT_YET_RECRUITING, ACTIVE_NOT_RECRUITING, COMPLETED, TERMINATED, WITHDRAWN, SUSPENDED, ENROLLING_BY_INVITATION, UNKNOWN, ... |
| `StudyType` | | INTERVENTIONAL, OBSERVATIONAL, EXPANDED_ACCESS |
| `LeadSponsorName`, `CollaboratorName` | organizations | free text |
| `LeadSponsorClass` | | INDUSTRY, NIH, FED, OTHER_GOV, NETWORK, INDIV, AMBIG, OTHER, UNKNOWN |
| `LocationCountry` | one entry per site | country names, e.g. "United States", "Korea, Republic of" |
| `StartDate`, `CompletionDate` | `YYYY-MM` or `YYYY-MM-DD`; future dates are estimates | dates |
| `EnrollmentCount` | participants | number |
| `BriefTitle` | | free text |

The tools accept any field the API documents; an unknown name returns close matches.

## Recipes
- **Trials of a drug** (drug is a listed intervention, synonyms included):
  `AREA[InterventionName]"x" OR AREA[InterventionOtherName]"x"`. Plain `query_intr` is broader.
- **A drug class** (e.g. PD-1 inhibitors): the registry has no class field. Find the member drugs
  that actually appear: `count_by` on `InterventionName` for a query such as
  `query_intr: "PD-1"`, then search for the members by name with an OR of field-scoped terms.
  List the members you chose in `assumptions`.
- **Excluding something**: `NOT` in the query removes trials (`NOT AREA[InterventionName]"x"`
  drops every trial listing x). To keep the trials but leave a value off the chart, use
  `drop_values` instead (e.g. drop pembrolizumab from a bar chart of drugs).
- **Scoping a chart's values**: `keep_values` / `drop_values` on a dimension. Drug and condition
  values are normalized names: lowercase generic drug names ("nivolumab") and lowercase disease
  names. Phases, statuses and sponsor classes use the enum values above.
- **Years**: `AREA[StartDate]RANGE[2015-01-01,MAX]`.
- **Recruiting trials in a country**: `filter_overall_status: [RECRUITING]` and
  `AREA[LocationCountry]"Germany"`.

## Pitfalls
- A trial counts toward every phase, country, condition or drug it lists.
- About 12% of trials have no phase and many have no sites listed; charts report them as excluded.
- Current-year and future start dates include planned (estimated) starts.
- Drug charts include every drug in a trial, so background chemotherapy often outranks the drug
  of interest; use `keep_values` when the question is about a specific group of drugs.
- Each tool call costs API requests (about one per second). Explore with intent; two or three
  searches are usually enough.
