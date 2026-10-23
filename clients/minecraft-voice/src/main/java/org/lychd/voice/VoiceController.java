package org.lychd.voice;

import java.io.IOException;
import java.util.Objects;
import java.util.UUID;
import java.util.function.LongSupplier;
import java.util.function.Supplier;

/** Small synchronized client actor. Tick owns physical input; two bounded lanes own all network IO. */
final class VoiceController {
    interface Lane { boolean offer(Runnable work); }
    record Hud(String requestId, String operation, String localStage, String phase, Protocol.Operation result) { }
    private static final byte[] EMPTY = new byte[0];
    private final ControlTransport transport;
    private final Lane ordinaryLane, stopLane;
    private final LongSupplier clock;
    private final Supplier<String> ids;
    private final EdgeGuard edges = new EdgeGuard();
    private Protocol.State state;
    private long stateAt = -1, contextVersion, nextPollAt;
    private EdgeGuard.Input input;
    private Work record, stop;
    private Hud hud;
    private boolean polling, closed, retired, sessionLost, stopSealed;
    private int records, stops;

    private static final class Work {
        final Protocol.Gesture gesture;
        final long pollUntil;
        Protocol.Operation observation;
        boolean dispatched, active = true, uncertain, stopRequested;
        Work(Protocol.Gesture gesture, long sessionExpiry) {
            this.gesture = gesture;
            pollUntil = gesture.operation().equals("stop") ? gesture.createdAt() + 60000
                    : Math.min(sessionExpiry, gesture.expiresAt() + 60000);
        }
    }

    VoiceController(ControlTransport transport, Lane ordinaryLane, Lane stopLane) {
        this(transport, ordinaryLane, stopLane, System::currentTimeMillis, () -> UUID.randomUUID().toString());
    }
    VoiceController(ControlTransport transport, Lane ordinaryLane, Lane stopLane, LongSupplier clock, Supplier<String> ids) {
        this.transport = transport; this.ordinaryLane = ordinaryLane; this.stopLane = stopLane;
        this.clock = clock; this.ids = ids;
    }

    synchronized void tick(EdgeGuard.Input next) {
        if (closed) return;
        input = next; long now = clock.getAsLong();
        EdgeGuard.Decision decision = edges.update(next, contextVersion);
        if (decision.boundary()) hud = null;
        if (decision.boundary() && record != null && !record.dispatched && record.active) cancelUnsent(record);
        if (decision.lostWorldOrFocus() && needsStop(record) && !record.stopRequested) requestStop(now);
        if (decision.action() == EdgeGuard.Action.STOP) requestStop(now);
        else if (decision.action() == EdgeGuard.Action.RECORD && recordEligible(now)) requestRecord(now);
        retireTimedOut(record, now); retireTimedOut(stop, now);
        poll(now);
    }

    private boolean fresh(long now) {
        return state != null && stateAt >= 0 && now >= stateAt && now - stateAt <= 1500 && now < state.sessionExpiresAt();
    }
    private boolean recordLifetimeFits(long now) {
        return state != null && now <= state.sessionExpiresAt() - state.instructionTimeout();
    }
    private boolean recordEligible(long now) {
        return !retired && records < 128 && input != null && input.matched() && input.focused()
                && input.menuFree() && !input.paused() && fresh(now) && recordLifetimeFits(now) && state.canRecord()
                && !state.stopping() && !state.audioBlocked() && !active(record)
                && (stop == null || stop.observation != null && stop.observation.status().equals("known")
                && stateAt >= stop.gesture.createdAt() && state.lastStop() != null
                && state.lastStop().requestId().equals(stop.gesture.requestId())
                && state.lastStop().status().equals("known"));
    }
    private void requestRecord(long now) {
        Work work = new Work(Protocol.gesture("record", state, now, edges.epoch(), ids.get()), state.sessionExpiresAt());
        record = work; records++;
        hud = new Hud(work.gesture.requestId(), "record", "requested", null, null);
        if (!ordinaryLane.offer(() -> mutate(work))) cancelUnsent(work);
    }
    private void requestStop(long now) {
        // Session expiry fences recording and context polling, never the reserved retirement stop.
        if (state == null || sessionLost || stopSealed || stops >= 64 || active(stop)) return;
        if (record != null && !record.dispatched && record.active) cancelUnsent(record);
        Work work = new Work(Protocol.gesture("stop", state, now, edges.epoch(), ids.get()), state.sessionExpiresAt());
        stop = work; stops++;
        if (record != null) record.stopRequested = true;
        hud = new Hud(work.gesture.requestId(), "stop", "requested", null, null);
        if (!stopLane.offer(() -> mutate(work))) { work.active = false; work.uncertain = true; local(work, "unknown"); }
    }
    private static boolean active(Work work) { return work != null && work.active; }
    private static boolean needsStop(Work work) { return work != null && work.dispatched && (work.active || work.uncertain); }
    private void cancelUnsent(Work work) { work.active = false; local(work, "not_sent"); }

    private void mutate(Work work) {
        synchronized (this) {
            long now = clock.getAsLong();
            if (!work.active) return;
            if (now < work.gesture.createdAt() || now >= work.gesture.admitBefore()) {
                work.active = false; local(work, "not_sent"); return;
            }
            if (work.gesture.operation().equals("record") && (closed || work.gesture.epoch() != edges.epoch()
                    || !recordEligibleForDispatch(work, now))) { cancelUnsent(work); return; }
            work.dispatched = true;
        }
        try {
            ControlTransport.Response response = transport.request("POST", "/v1/" + work.gesture.operation(), work.gesture.bytes());
            synchronized (this) {
                if (response.status() == 202) accept(work, Protocol.operation(response.body()));
                else if (isRefusal(response)) { work.active = false; local(work, "refused"); }
                else unknown(work);
            }
        } catch (IOException | RuntimeException exception) { synchronized (this) { unknown(work); } }
    }
    private boolean recordEligibleForDispatch(Work work, long now) {
        return !retired && fresh(now) && recordLifetimeFits(now)
                && work.gesture.expiresAt() <= state.sessionExpiresAt() && now < work.gesture.expiresAt()
                && input != null && input.matched() && input.focused()
                && input.menuFree() && !input.paused() && state.canRecord() && !state.stopping() && !state.audioBlocked()
                && state.sessionId().equals(work.gesture.sessionId()) && state.contextId().equals(work.gesture.contextId())
                && state.generation() == work.gesture.generation() && !active(stop);
    }

    private static boolean isRefusal(ControlTransport.Response response) {
        return switch (response.status()) {
            case 400, 401, 408, 409, 429 -> Protocol.fixedStatus(response.body(), "refused");
            default -> false;
        };
    }
    private void unknown(Work work) { work.uncertain = true; local(work, "unknown"); }
    private void local(Work work, String stage) {
        if (!closed && work.gesture.epoch() == edges.epoch() && hud != null
                && hud.requestId().equals(work.gesture.requestId())) {
            hud = new Hud(work.gesture.requestId(), work.gesture.operation(), stage, null, work.observation);
        }
    }
    private void accept(Work work, Protocol.Operation observation) {
        if (!observation.requestId().equals(work.gesture.requestId())
                || !observation.operation().equals(work.gesture.operation())) { unknown(work); return; }
        if (work.observation != null && work.observation.terminal() && !observation.terminal()) return;
        work.observation = observation; work.uncertain = false;
        work.active = !(observation.operation().equals("stop") ? observation.terminal() : observation.settled());
        if (observation.status().equals("unknown")) {
            retired = true; work.active = false;
            if (work.gesture.operation().equals("stop")) stopSealed = true;
        }
        if (!closed && work.gesture.epoch() == edges.epoch() && hud != null
                && hud.requestId().equals(work.gesture.requestId())) {
            hud = new Hud(work.gesture.requestId(), work.gesture.operation(), observation.status(), observation.phase(), observation);
        }
    }
    private void retireTimedOut(Work work, long now) {
        if (active(work) && now >= work.pollUntil) {
            work.active = false; work.uncertain = true; retired = true; local(work, "unknown");
            if (work.gesture.operation().equals("stop")) stopSealed = true;
        }
    }

    private void poll(long now) {
        boolean sessionExpired = state != null && now >= state.sessionExpiresAt();
        if (polling || now < nextPollAt || (retired || sessionExpired) && !active(stop)) return;
        if (!(input.matched() && input.focused()) && !active(record) && !active(stop)) return;
        Work lookup = active(stop) && stop.dispatched ? stop : active(record) && record.dispatched ? record : null;
        if ((sessionExpired || retired) && lookup == null) return;
        // Alternate state and own-operation reads. One poll in flight across both lanes, at most 2 Hz.
        boolean operationRead = lookup != null && (sessionExpired || retired || state == null || now - stateAt < 1000);
        Work selected = operationRead ? lookup : null;
        String path = selected == null ? "/v1/state" : "/v1/operations/" + selected.gesture.requestId();
        long queryEpoch = edges.epoch();
        Lane lane = selected != null && selected.gesture.operation().equals("stop") ? stopLane : ordinaryLane;
        polling = true; nextPollAt = now + 500;
        if (!lane.offer(() -> query(path, selected, queryEpoch))) polling = false;
    }
    private void query(String path, Work selected, long queryEpoch) {
        try {
            ControlTransport.Response response = transport.request("GET", path, EMPTY);
            synchronized (this) {
                if (closed) return;
                if (response.status() == 200) {
                    if (selected != null) accept(selected, Protocol.operation(response.body()));
                    else updateState(Protocol.state(response.body()), queryEpoch);
                } else if (selected != null) {
                    if (response.status() == 404 && Protocol.fixedStatus(response.body(), "not_found")) {
                        selected.active = false; unknown(selected); retired = true;
                        if (selected.gesture.operation().equals("stop")) stopSealed = true;
                    } else unknown(selected);
                } else stateAt = -1;
            }
        } catch (IOException | RuntimeException exception) {
            synchronized (this) { if (selected != null) unknown(selected); else stateAt = -1; }
        } finally {
            synchronized (this) {
                polling = false;
                nextPollAt = Math.max(nextPollAt, clock.getAsLong() + 500);
            }
        }
    }
    private void updateState(Protocol.State next, long queryEpoch) {
        if (state != null && !state.sessionId().equals(next.sessionId())) {
            retired = true; sessionLost = true; contextVersion++; stateAt = -1;
            if (record != null) unknown(record); if (stop != null) unknown(stop);
            return;
        }
        if (state == null || !Objects.equals(state.contextId(), next.contextId()) || state.generation() != next.generation()) contextVersion++;
        state = next; stateAt = clock.getAsLong();
        if (record != null && next.lastRecord() != null && next.lastRecord().requestId().equals(record.gesture.requestId())) accept(record, next.lastRecord());
        if (stop != null && next.lastStop() != null && next.lastStop().requestId().equals(stop.gesture.requestId())) accept(stop, next.lastStop());
        if (queryEpoch == edges.epoch() && record != null && record.gesture.epoch() == edges.epoch()
                && next.voiceRequestId() != null && next.voiceRequestId().equals(record.gesture.requestId())
                && hud != null && hud.requestId().equals(record.gesture.requestId()) && record.observation == null) {
            hud = new Hud(record.gesture.requestId(), "record", "pending", next.voicePhase(), null);
        }
    }

    synchronized Hud hud() { return hud; }
    /** Close ingress first. The owner then drains the reserved stop lane and closes all IO. */
    synchronized void beginClose() {
        if (closed) return;
        if (needsStop(record) && !record.stopRequested) requestStop(clock.getAsLong());
        closed = true;
        if (record != null && !record.dispatched) record.active = false;
    }
}
