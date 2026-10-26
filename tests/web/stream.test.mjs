import {test} from "node:test";
import assert from "node:assert/strict";
import {readFileSync} from "node:fs";
const source = readFileSync(new URL("../../coscientist/api/web/js/stream.js",import.meta.url),"utf8");
const {StudyStream} = await import(`data:text/javascript;base64,${Buffer.from(source).toString("base64")}`);
globalThis.location = {protocol:"http:",host:"localhost"};
class Socket { static instances=[]; constructor(url){this.url=url;Socket.instances.push(this);} close(){} }
globalThis.WebSocket = Socket;

test("queued callback from stopped study cannot deliver events or change cursor",()=>{
 const batches=[]; const stream=new StudyStream("A", batch=>batches.push(batch),()=>{});
 stream.start(); const socket=Socket.instances.at(-1), queued=socket.onmessage;
 stream.stop(); queued({data:JSON.stringify({seq:999,study_id:"A"})});
 assert.equal(batches.length,0); assert.equal(stream.lastSeq,0);
 stream.start(); queued({data:JSON.stringify({seq:999,study_id:"A"})});
 assert.equal(batches.length,0); stream.stop();
});

test("repeated start owns one socket and duplicate resume event is ignored",()=>{
 const batches=[]; const stream=new StudyStream("B", batch=>batches.push(batch),()=>{});
 stream.start(); const socket=stream.ws; stream.start(); assert.equal(stream.ws,socket);
 const message={data:JSON.stringify({seq:5,study_id:"B"})}; socket.onmessage(message); socket.onmessage(message);
 assert.equal(batches.length,1); assert.equal(stream.lastSeq,5); stream.stop();
});
