export interface Topic {
  number: number;
  label: string;
}
export interface MapPoint {
  slug: string;
  title: string;
  primaryArea: string | null;
  x: number;
  y: number;
  region: number | null;
  cluster: number | null;
}
export interface PaperMap {
  kind: string;
  slugs: string[];
  x: number[];
  y: number[];
  region: (number | null)[];
  cluster: (number | null)[];
  area: (number | null)[];
  areas: string[];
  regions: Topic[];
  clusters: Topic[];
  /** `all` when the payload spans every year in the corpus. */
  year_mode?: 'single' | 'all';
  /** Publication year per point; only sent for `year=all` payloads. */
  years?: (number | null)[];
}
export interface MapTitles {
  titles: Record<string, string>;
}
export interface Related {
  related: { id: string; slug: string; title: string }[];
  hidden: number;
}
export interface MapSearch {
  slugs: string[];
  errors: string[];
}
export interface MapRelated {
  slugs: string[];
  edges: number[];
}
export interface MinimapData {
  points: MapPoint[];
  vectors: number[][] | null;
  self: number;
  related: number[];
  relatedElsewhere: number;
  edges: [number, number][];
  clusters: Topic[];
}

export interface SemanticItem {
  id: string;
  slug: string;
  title: string;
  abstract: string;
  authors: string[];
  venues: { id: string; name: string; year: number; relation_type: string }[];
  year: number;
  score: number;
  /** Optional links, added for copy/export; absent on older payloads. */
  doi?: string | null;
  paper_url?: string | null;
  pdf_url?: string | null;
}

export interface SemanticResult {
  items: SemanticItem[];
  method: string;
  note: string | null;
  query: string;
}

export interface GapPaper {
  id: string;
  title: string;
  abstract: string;
}

export interface Gap {
  type: 'bridge' | 'sparse' | 'cue';
  title: string;
  explanation: string;
  score: number;
  paper_ids: string[];
  papers: GapPaper[];
}

export interface TopicSummary {
  number: number;
  label: string;
  size: number;
  top_terms: string[];
}

export interface GapsResult {
  gaps: Gap[];
  topics: TopicSummary[];
}
