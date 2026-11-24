import { notFound } from "next/navigation";
import { McxInstrumentDetail, MCX_NAMES } from "@/components/mcx-futures";
import { PageHeading } from "@/components/ui";
import { isMcxInstrument, MCX_INSTRUMENTS } from "@/types/mcx-futures";

/** Only the two configured instruments exist; anything else is a 404. */
export const dynamicParams = false;
export function generateStaticParams() {
  return MCX_INSTRUMENTS.map(instrument => ({ instrument }));
}

export default async function McxInstrumentPage({ params }: { params: Promise<{ instrument: string }> }) {
  const { instrument } = await params;
  if (!isMcxInstrument(instrument)) notFound();
  return <main className="mcx-futures ast-futures">
    <PageHeading eyebrow="MCX futures · PAPER" title={MCX_NAMES[instrument]}
      description={`${instrument} futures paper engine: decision timeline, paper trade blotter and forward paper performance. Times in Asia/Kolkata.`}/>
    <McxInstrumentDetail instrument={instrument}/>
  </main>;
}
