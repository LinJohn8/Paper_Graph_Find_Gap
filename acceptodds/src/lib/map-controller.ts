import { OrthographicController } from '@deck.gl/core';

/**
 * deck's controller, except that a drag always pans. Its own turns a drag
 * with ⌘, Ctrl, Alt or Shift held into a rotation, which a flat
 * (orthographic) view cannot do, so the drag did nothing — and the minimap
 * zooms with ⌘ held, so a drag straight after a zoom would be lost.
 */
export class PanController extends OrthographicController {
  isFunctionKeyPressed(event: Parameters<OrthographicController['isFunctionKeyPressed']>[0]): boolean {
    return event.type === 'panstart' ? false : super.isFunctionKeyPressed(event);
  }

  protected _onPinchEnd(event: Parameters<OrthographicController['_onPinchEnd']>[0]): boolean {
    // deck's pinch inertia blocks new gestures for its whole 300ms transition.
    // Repeated touch gestures then feel frozen (and can keep zooming the wrong
    // way). End a touch pinch at the fingers; retain inertia for ordinary pans.
    if (event.pointerType !== 'touch') return super._onPinchEnd(event);
    const inertia = this.inertia;
    this.inertia = 0;
    try {
      return super._onPinchEnd(event);
    } finally {
      this.inertia = inertia;
    }
  }
}

/**
 * Embedded in a page that scrolls, on a touch screen one finger scrolls the
 * page and two pan and zoom, as an embedded Google map does: the browser
 * keeps one-finger pans (`touch-action: pan-x pan-y` in MapCanvas), and a pinch
 * pans as it zooms, around the fingers. Without this the figure caught every
 * swipe and the page could not be scrolled past it.
 */
export class EmbeddedController extends PanController {
  protected _onPanStart(event: Parameters<PanController['_onPanStart']>[0]): boolean {
    return event.pointerType === 'touch' ? false : super._onPanStart(event);
  }
}
