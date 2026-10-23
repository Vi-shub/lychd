package org.lychd.voice;

import org.junit.jupiter.api.Test;
import static org.junit.jupiter.api.Assertions.*;

class EdgeGuardTest {
    private final Object world = new Object();
    private EdgeGuard.Input input(boolean focus, boolean menu, boolean rec, boolean stop, boolean safe, boolean same) {
        return new EdgeGuard.Input(world, true, focus, menu, false, rec, stop, safe, same);
    }
    @Test void heldInputAndFocusReturnNeverReplayAnEdge() {
        EdgeGuard edges = new EdgeGuard();
        assertEquals(EdgeGuard.Action.NONE, edges.update(input(true, true, true, false, true, false), 1).action());
        edges.update(input(true, true, false, false, true, false), 1);
        assertEquals(EdgeGuard.Action.RECORD, edges.update(input(true, true, true, false, true, false), 1).action());
        assertEquals(EdgeGuard.Action.NONE, edges.update(input(true, true, true, false, true, false), 1).action());
        assertTrue(edges.update(input(false, true, true, false, true, false), 1).lostWorldOrFocus());
        assertEquals(EdgeGuard.Action.NONE, edges.update(input(true, true, true, false, true, false), 1).action());
        edges.update(input(true, true, false, false, true, false), 1);
        assertEquals(EdgeGuard.Action.RECORD, edges.update(input(true, true, true, false, true, false), 1).action());
    }
    @Test void menuAndContextTransitionsRequireReleaseButNonTextStopWorksInMenus() {
        EdgeGuard edges = new EdgeGuard();
        edges.update(input(true, true, false, false, true, false), 1);
        edges.update(input(true, false, false, false, true, false), 1);
        assertEquals(EdgeGuard.Action.STOP, edges.update(input(true, false, false, true, true, false), 1).action());
        edges.update(input(true, false, false, false, false, false), 1);
        assertEquals(EdgeGuard.Action.NONE, edges.update(input(true, false, false, true, false, false), 1).action());
        edges.update(input(true, true, false, false, true, false), 1);
        assertEquals(EdgeGuard.Action.NONE, edges.update(input(true, true, true, false, true, false), 2).action());
        assertEquals(EdgeGuard.Action.NONE, edges.update(input(true, true, true, false, true, false), 2).action());
    }
    @Test void simultaneousAndSameBindingsChooseStopAndWorldReplacementInvalidates() {
        EdgeGuard edges = new EdgeGuard();
        edges.update(input(true, true, false, false, true, false), 1);
        assertEquals(EdgeGuard.Action.STOP, edges.update(input(true, true, true, true, true, false), 1).action());
        edges.update(input(true, true, false, false, true, true), 1);
        assertEquals(EdgeGuard.Action.STOP, edges.update(input(true, true, true, false, true, true), 1).action());
        EdgeGuard.Decision changed = edges.update(new EdgeGuard.Input(new Object(), true, true, true, false, true, true, true, true), 1);
        assertTrue(changed.boundary()); assertTrue(changed.lostWorldOrFocus()); assertEquals(EdgeGuard.Action.NONE, changed.action());
    }
}
