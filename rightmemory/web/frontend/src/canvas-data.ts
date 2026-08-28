import type { MindElixirData, NodeObj } from 'mind-elixir';
import { indexTree, type Snapshot } from './tree.ts';
import { rootBranchSide, type ViewState } from './view-state.ts';

export const palette = ['#d99470', '#d8757d', '#79a494', '#9694b9', '#80a7b8', '#b895ae'];

/** Each canvas tree starts at a stored direction; there is no invented parent. */
export function forestData(snapshot: Snapshot, view: ViewState): MindElixirData[] {
  const items = indexTree(snapshot);
  const folded = new Set(view.collapsed);
  const build = (id: string, depth: number, color: string): NodeObj => {
    const item = items.get(id)!;
    return {
      id, topic: item.title, expanded: !folded.has(id), branchColor: color,
      style: depth === 0
        ? { color: '#243633', fontSize: '25px', background: '#ffffff', border: 'none', fontWeight: '650' }
        : depth === 1
          ? { color: '#293c37', background: `${color}26`, border: 'none', fontSize: '16px', fontWeight: '550' }
          : { color: '#35453f', background: depth === 2 ? `${color}14` : 'transparent', border: 'none', fontSize: '14px' },
      children: depth === 0 && folded.has(id) ? [] : item.child_ids.map((child, index) => ({
        ...build(child, depth + 1, depth === 0 ? palette[index % palette.length] : color),
        ...(depth === 0 ? { direction: rootBranchSide(index, item.child_ids.length) === 'left' ? 0 as const : 1 as const } : {}),
      })),
    };
  };
  return snapshot.root_ids.map((id) => ({
    nodeData: build(id, 0, palette[0]),
    direction: items.get(id)!.child_ids.length > 1 ? 2 : 1,
  }));
}

export { titleMarkup, titleText } from './title-format.ts';

export interface MapSize { id: string; width: number; height: number; rootX: number }

/** Align real roots vertically, preserving order and leaving space between maps. */
export function stackMaps(sizes: MapSize[], gap = 64): { maps: (MapSize & { x: number; y: number })[]; width: number; height: number } {
  const axis = Math.max(0, ...sizes.map((size) => size.rootX));
  let height = 0;
  let width = 0;
  const maps = sizes.map((size) => {
    const map = { ...size, x: axis - size.rootX, y: height };
    width = Math.max(width, map.x + size.width);
    height += size.height + gap;
    return map;
  });
  return { maps, width, height: maps.length ? height - gap : 0 };
}
