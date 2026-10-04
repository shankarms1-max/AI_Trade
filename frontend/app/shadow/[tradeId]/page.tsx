import { ShadowTradeDetail } from "@/components/shadow-pages";
export default async function Page({params}:{params:Promise<{tradeId:string}>}){const {tradeId}=await params;return <ShadowTradeDetail id={Number(tradeId)}/>}
