import type { Metadata } from 'next';
import { PaperHeader } from '@/components/PaperHeader';
import './globals.css';
export const metadata: Metadata = {
  title: { default: 'Paper Graph', template: '%s | Paper Graph' },
  description: 'Personal Academic Paper Explorer',
};
export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body>
        <PaperHeader />
        {children}
      </body>
    </html>
  );
}
