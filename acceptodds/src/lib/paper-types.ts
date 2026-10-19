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
