import {cleanup,render,screen} from '@testing-library/react';
import {afterEach,expect,it} from 'vitest';
import {ResponsiveTable} from '@/components/responsive-table';
import {statusTone} from '@/components/ui';
afterEach(cleanup);
it('labels mobile record fields using their actual table headings, preserving missing values',()=>{
 render(<ResponsiveTable aria-label="Stored evidence"><thead><tr><th>Strategy</th><th>Gross P&L</th></tr></thead><tbody><tr><td>ORB 15</td><td>N/A</td></tr></tbody></ResponsiveTable>);
 const cells=screen.getAllByRole('cell');expect(cells[0]).toHaveAttribute('data-label','Strategy');expect(cells[1]).toHaveAttribute('data-label','Gross P&L');expect(cells[1]).toHaveTextContent('N/A');expect(screen.queryByText('0')).not.toBeInTheDocument();
});
it.each([['NO_SETUP','neutral'],['WATCH','accent'],['ENTER','accent'],['BLOCKED','warn'],['ENTRY_REJECTED','warn'],['OPEN','accent'],['CLOSED','neutral'],['UNRESOLVED_EXPOSURE','bad'],['CONTRACT_EXPIRED_UNPRICED','warn'],['STALE','warn'],['UNAVAILABLE','neutral'],['HEALTHY','good']])('gives %s a consistent semantic tone', (state,tone)=>{expect(statusTone(state)).toBe(tone);});
