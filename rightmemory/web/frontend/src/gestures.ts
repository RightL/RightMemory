import type { ViewState } from './view-state.ts';

type Viewport = NonNullable<ViewState['viewport']>;
interface Point { x: number; y: number }
export interface PointerSample extends Point { pointerId: number; pointerType: string; button: number; buttons: number }
type Motion = { kind: 'idle' } | { kind: 'cancel' } | { kind: 'view'; viewport: Viewport } | { kind: 'drag'; id: string };

/** Touch changes the view; only a held mouse/pen press can produce a node drop. */
export class CanvasGestures {
  private drag: { pointer: number; id: string | null; start: Point; viewport: Viewport; buttons: number; moved: boolean } | null = null;
  private touches = new Map<number, Point>();
  private touchOrigin: { center: Point; distance: number; viewport: Viewport } | null = null;

  get touching(): boolean { return this.touches.size > 0; }
  get active(): boolean { return !!this.drag || this.touching; }
  has(pointer: number): boolean { return this.drag?.pointer === pointer || this.touches.has(pointer); }

  start(event: PointerSample, id: string | null, viewport: Viewport): boolean {
    if (this.has(event.pointerId)) this.cancel();
    if (event.pointerType === 'touch') {
      this.drag = null;
      this.touches.set(event.pointerId, { x: event.x, y: event.y });
      this.rebaseTouch(viewport);
      return true;
    }
    if (this.active || !event.buttons) return false;
    const buttons = event.button === 1 ? 4 : event.button === 2 ? 2 : 2 ** event.button;
    this.drag = { pointer: event.pointerId, id: event.button === 0 ? id : null, start: { x: event.x, y: event.y }, viewport: { ...viewport }, buttons, moved: false };
    return true;
  }

  move(event: PointerSample): Motion {
    if (!this.has(event.pointerId)) return { kind: 'idle' };
    if (!event.buttons || this.drag && !(event.buttons & this.drag.buttons)) {
      this.cancel();
      return { kind: 'cancel' };
    }
    if (this.touches.has(event.pointerId)) {
      this.touches.set(event.pointerId, { x: event.x, y: event.y });
      const current = this.touchGeometry();
      const start = this.touchOrigin!;
      const scale = Math.max(0.1, Math.min(2, start.viewport.scale * (start.distance ? current.distance / start.distance : 1)));
      const ratio = scale / start.viewport.scale;
      return { kind: 'view', viewport: {
        x: current.center.x - (start.center.x - start.viewport.x) * ratio,
        y: current.center.y - (start.center.y - start.viewport.y) * ratio,
        scale,
      } };
    }
    const drag = this.drag!;
    const dx = event.x - drag.start.x;
    const dy = event.y - drag.start.y;
    if (!drag.moved && Math.hypot(dx, dy) < 5) return { kind: 'idle' };
    drag.moved = true;
    return drag.id ? { kind: 'drag', id: drag.id }
      : { kind: 'view', viewport: { ...drag.viewport, x: drag.viewport.x + dx, y: drag.viewport.y + dy } };
  }

  end(pointer: number, viewport: Viewport): string | null {
    if (this.touches.delete(pointer)) { this.rebaseTouch(viewport); return null; }
    if (this.drag?.pointer !== pointer) return null;
    const id = this.drag.moved ? this.drag.id : null;
    this.drag = null;
    return id;
  }

  cancel(): void { this.drag = null; this.touches.clear(); this.touchOrigin = null; }

  private touchGeometry(): { center: Point; distance: number } {
    const [first, second] = [...this.touches.values()];
    return second ? {
      center: { x: (first.x + second.x) / 2, y: (first.y + second.y) / 2 },
      distance: Math.hypot(second.x - first.x, second.y - first.y),
    } : { center: first, distance: 0 };
  }

  private rebaseTouch(viewport: Viewport): void {
    this.touchOrigin = this.touching ? { ...this.touchGeometry(), viewport: { ...viewport } } : null;
  }
}
