import { SnapshotDetail } from "@/components/snapshot-detail";
export default async function Page({params}:{params:Promise<{snapshotId:string}>}){const {snapshotId}=await params;return <SnapshotDetail id={Number(snapshotId)}/>}
