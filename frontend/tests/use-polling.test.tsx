import { act,cleanup,renderHook } from "@testing-library/react";
import { afterEach,beforeEach,expect,it,vi } from "vitest";
import { usePolling } from "@/hooks/use-polling";
let visibility="visible";
beforeEach(()=>{vi.useFakeTimers();vi.spyOn(document,"visibilityState","get").mockImplementation(()=>visibility as DocumentVisibilityState);visibility="visible";});
afterEach(()=>{cleanup();vi.restoreAllMocks();vi.useRealTimers();});
it("pauses hidden-document reads and immediately refreshes on foreground",async()=>{
 const loader=vi.fn().mockResolvedValue("observed");renderHook(()=>usePolling(loader,15000));
 await act(()=>vi.advanceTimersByTimeAsync(1));expect(loader).toHaveBeenCalledTimes(1);
 visibility="hidden";await act(()=>vi.advanceTimersByTimeAsync(60000));expect(loader).toHaveBeenCalledTimes(1);
 visibility="visible";await act(async()=>{document.dispatchEvent(new Event("visibilitychange"));});expect(loader).toHaveBeenCalledTimes(2);
});
it("never overlaps reads; one queued refresh follows a slow request",async()=>{
 let done!:(v:number)=>void;const loader=vi.fn().mockImplementationOnce(()=>new Promise<number>(resolve=>{done=resolve;})).mockResolvedValue(2);
 const {result}=renderHook(()=>usePolling(loader,15000));await act(()=>vi.advanceTimersByTimeAsync(45001));expect(loader).toHaveBeenCalledTimes(1);
 await act(async()=>{done(1);});expect(loader).toHaveBeenCalledTimes(2);expect(result.current.data).toBe(2);
});
it("ignores a late response from the previous selected scope",async()=>{
 let done!:(v:string)=>void;const old=()=>new Promise<string>(resolve=>{done=resolve;});const selected=vi.fn().mockResolvedValue("current");
 const {result,rerender}=renderHook(({loader})=>usePolling(loader,15000),{initialProps:{loader:old}});
 await act(()=>vi.advanceTimersByTimeAsync(1));rerender({loader:selected});await act(()=>vi.advanceTimersByTimeAsync(1));await act(async()=>{done("old");});expect(result.current.data).toBe("current");
});
it("does not duplicate reads for supplied backend projections",async()=>{
 const loader=vi.fn();renderHook(()=>usePolling(loader,15000,false));await act(()=>vi.advanceTimersByTimeAsync(30000));expect(loader).not.toHaveBeenCalled();
});
