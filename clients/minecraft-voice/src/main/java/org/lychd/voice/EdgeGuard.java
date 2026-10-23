package org.lychd.voice;

/** Pure physical edge state. Every boundary requires release before another action. */
final class EdgeGuard {
    enum Action { NONE, RECORD, STOP }
    record Input(Object world, boolean matched, boolean focused, boolean menuFree, boolean paused,
                 boolean recordDown, boolean stopDown, boolean menuStopSafe, boolean sameBinding) { }
    private Object world;
    private boolean matched, focused, menuFree, paused, initialized, armed, recordDown, stopDown;
    private long contextVersion = -1;
    private long epoch;

    record Decision(Action action, boolean lostWorldOrFocus, boolean boundary, long epoch) { }
    Decision update(Input input, long context) {
        boolean boundary = !initialized || world != input.world || matched != input.matched || focused != input.focused
                || menuFree != input.menuFree || paused != input.paused || contextVersion != context;
        boolean lost = initialized && matched && focused && (!input.matched || !input.focused || world != input.world);
        if (boundary) { epoch++; armed = false; }
        Action action = Action.NONE;
        if (!input.recordDown && !input.stopDown) armed = true;
        else if (armed && !boundary && input.matched && input.focused) {
            boolean stopEdge = input.stopDown && !stopDown;
            boolean recordEdge = input.recordDown && !recordDown;
            if ((stopEdge || input.sameBinding && recordEdge) && (input.menuFree || input.menuStopSafe)) action = Action.STOP;
            else if (recordEdge && !input.stopDown && input.menuFree && !input.paused) action = Action.RECORD;
        }
        world = input.world; matched = input.matched; focused = input.focused; menuFree = input.menuFree;
        paused = input.paused; recordDown = input.recordDown; stopDown = input.stopDown;
        contextVersion = context; initialized = true;
        return new Decision(action, lost, boundary, epoch);
    }
    long epoch() { return epoch; }
}
