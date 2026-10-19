import { afterEach, describe, expect, it, vi } from 'vitest';
import { OrthographicViewport } from '@deck.gl/core';
import { EventManager } from 'mjolnir.js';
import { Timeline } from '@luma.gl/engine';
import { EmbeddedController, PanController } from '@/lib/map-controller';

type Gesture = Parameters<PanController['handleEvent']>[0];
function pinch(type: string, scale: number, deltaTime: number): Gesture {
  return {
    type,
    pointerType: 'touch',
    offsetCenter: { x: 195, y: 350 },
    scale,
    rotation: 0,
    deltaTime,
    srcEvent: {},
    stopPropagation() {},
  } as Gesture;
}

afterEach(() => vi.useRealTimers());

describe.each([
  { name: 'full map', Controller: PanController },
  { name: 'minimap', Controller: EmbeddedController },
])('$name touch interaction', ({ Controller }) => {
  it('accepts another pinch immediately after a moving pinch ends', () => {
    vi.useFakeTimers();
    const onViewStateChange = vi.fn();
    const controller = new Controller({
      timeline: new Timeline(),
      eventManager: new EventManager(),
      makeViewport: (props) => new OrthographicViewport(props),
      onViewStateChange,
      onStateChange() {},
    });
    controller.setProps({
      id: 'map',
      x: 0,
      y: 0,
      width: 390,
      height: 700,
      target: [500, 500, 0],
      zoom: 0,
      inertia: true,
    });
    try {
      for (let i = 0; i < 10; i++) {
        expect(controller.handleEvent(pinch('pinchstart', 1, 0)), `pinch ${i + 1} was ignored`).toBe(true);
        expect(controller.handleEvent(pinch('pinchmove', 1.2, 16))).toBe(true);
        expect(controller.handleEvent(pinch('pinchend', 1.3, 32))).toBe(true);
        expect(controller.isDragging()).toBe(false);
        vi.advanceTimersByTime(50);
      }
      // A touch pinch must not disable subsequent mouse dragging or its inertia.
      const drag = { ...pinch('panstart', 1, 0), pointerType: 'mouse', srcEvent: { ctrlKey: true } } as Gesture;
      expect(controller.handleEvent(drag)).toBe(true);
      expect(
        controller.handleEvent({ ...drag, type: 'panend', velocity: 1, velocityX: 1, velocityY: 0 } as Gesture),
      ).toBe(true);
      expect(onViewStateChange.mock.lastCall?.[0].viewState.transitionDuration).toBeGreaterThan(0);
      // One finger still scrolls the page past the embedded minimap.
      expect(controller.handleEvent(pinch('panstart', 1, 0))).toBe(Controller === PanController);
    } finally {
      controller.finalize();
    }
  });
});
