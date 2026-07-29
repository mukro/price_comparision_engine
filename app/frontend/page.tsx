// app/products/[id]/page.tsx
'use client';

import { AlertCircle, ArrowUpRight, CheckCircle, Tag, TrendingDown } from 'lucide-react';
import { useEffect, useState } from 'react';
import { Legend, Line, LineChart, ResponsiveContainer, Tooltip, XAxis, YAxis } from 'recharts';

interface VendorOffer {
  offer_id: string;
  vendor_name: string;
  vendor_domain: string;
  raw_title: string;
  current_price: number;
  currency: string;
  in_stock: boolean;
  buy_url: string;
  last_scraped_at: string;
}

interface PriceGridData {
  product_id: string;
  title: string;
  brand?: string;
  model_code?: string;
  image_url?: string;
  specifications: Record<string, string>;
  offers: VendorOffer[];
}

interface PricePoint {
  vendor_name: string;
  price: number;
  recorded_at: string;
}

export default function ProductComparisonPage({ params }: { params: { id: string } }) {
  const [gridData, setGridData] = useState<PriceGridData | null>(null);
  const [priceHistory, setPriceHistory] = useState<PricePoint[]>([]);
  const [loading, setLoading] = useState(true);
  const API_BASE = process.env.NEXT_PUBLIC_API_URL || 'http://localhost:8000/api/v1';

  useEffect(() => {
    async function fetchData() {
      try {
        const [gridRes, historyRes] = await Promise.all([
          fetch(`${API_BASE}/products/${params.id}/grid`),
          fetch(`${API_BASE}/products/${params.id}/history?days=30`),
        ]);

        if (gridRes.ok) {
          const grid = await gridRes.json();
          setGridData(grid);
        }
        if (historyRes.ok) {
          const history = await historyRes.json();
          setPriceHistory(history.history);
        }
      } catch (err) {
        console.error('Failed to load comparison data:', err);
      } finally {
        setLoading(false);
      }
    }

    fetchData();
  }, [params.id]);

  if (loading) {
    return (
      <div className="flex justify-center items-center min-h-screen text-gray-500 font-medium">
        Loading real-time prices across vendors...
      </div>
    );
  }

  if (!gridData) {
    return (
      <div className="flex justify-center items-center min-h-screen text-red-500 font-medium">
        Product comparison data could not be retrieved.
      </div>
    );
  }

  const lowestPrice = gridData.offers.length > 0 ? gridData.offers[0].current_price : null;

  return (
    <div className="max-w-6xl mx-auto px-4 py-8 space-y-8 font-sans">
      
      {/* Product Overview Header */}
      <div className="flex flex-col md:flex-row gap-6 bg-white p-6 rounded-xl border border-gray-200 shadow-sm">
        {gridData.image_url && (
          <div className="w-full md:w-48 h-48 flex-shrink-0 bg-gray-50 rounded-lg p-2 flex items-center justify-center">
            <img src={gridData.image_url} alt={gridData.title} className="max-h-full object-contain" />
          </div>
        )}
        <div className="flex-1 space-y-2">
          {gridData.brand && (
            <span className="inline-block text-xs font-semibold uppercase tracking-wider text-indigo-600 bg-indigo-50 px-2.5 py-1 rounded">
              {gridData.brand}
            </span>
          )}
          <h1 className="text-2xl font-bold text-gray-900">{gridData.title}</h1>
          <p className="text-sm text-gray-500">Model: {gridData.model_code || 'N/A'}</p>

          {lowestPrice && (
            <div className="pt-2 flex items-baseline gap-2">
              <span className="text-sm text-gray-500">Starting from:</span>
              <span className="text-3xl font-extrabold text-emerald-600">${lowestPrice.toFixed(2)}</span>
            </div>
          )}
        </div>
      </div>

      {/* Real-time Price Comparison Matrix */}
      <div className="bg-white rounded-xl border border-gray-200 shadow-sm overflow-hidden">
        <div className="p-5 border-b border-gray-200 flex items-center justify-between">
          <div className="flex items-center gap-2">
            <Tag className="w-5 h-5 text-indigo-600" />
            <h2 className="text-lg font-bold text-gray-900">Compare Prices across Service Providers</h2>
          </div>
          <span className="text-xs text-gray-500">{gridData.offers.length} Providers Found</span>
        </div>

        <div className="divide-y divide-gray-100">
          {gridData.offers.map((offer, idx) => {
            const isBestDeal = idx === 0;
            return (
              <div
                key={offer.offer_id}
                className={`p-4 md:p-5 flex flex-col md:flex-row md:items-center justify-between gap-4 transition-colors ${
                  isBestDeal ? 'bg-emerald-50/50' : 'hover:bg-gray-50'
                }`}
              >
                {/* Vendor Metadata */}
                <div className="flex items-center gap-4 min-w-[200px]">
                  <div>
                    <div className="flex items-center gap-2">
                      <span className="font-bold text-gray-900">{offer.vendor_name}</span>
                      {isBestDeal && (
                        <span className="inline-flex items-center gap-1 text-[10px] font-bold text-emerald-700 bg-emerald-100 px-2 py-0.5 rounded-full">
                          <TrendingDown className="w-3 h-3" /> Best Price
                        </span>
                      )}
                    </div>
                    <span className="text-xs text-gray-400">{offer.vendor_domain}</span>
                  </div>
                </div>

                {/* Stock Status */}
                <div className="flex items-center gap-1 text-xs min-w-[120px]">
                  {offer.in_stock ? (
                    <span className="flex items-center gap-1 text-emerald-600 font-medium">
                      <CheckCircle className="w-4 h-4" /> In Stock
                    </span>
                  ) : (
                    <span className="flex items-center gap-1 text-amber-600 font-medium">
                      <AlertCircle className="w-4 h-4" /> Out of Stock
                    </span>
                  )}
                </div>

                {/* Price Point */}
                <div className="text-left md:text-right min-w-[120px]">
                  <span className="text-xl font-black text-gray-900">
                    ${offer.current_price.toFixed(2)}
                  </span>
                  <span className="block text-[10px] text-gray-400">
                    Updated {new Date(offer.last_scraped_at).toLocaleDateString()}
                  </span>
                </div>

                {/* Direct Purchasing Button */}
                <div>
                  <a
                    href={offer.buy_url}
                    target="_blank"
                    rel="noopener noreferrer"
                    className={`inline-flex items-center justify-center gap-1 text-sm font-semibold px-4 py-2 rounded-lg transition-all ${
                      isBestDeal
                        ? 'bg-emerald-600 text-white hover:bg-emerald-700 shadow-sm'
                        : 'bg-gray-900 text-white hover:bg-gray-800'
                    }`}
                  >
                    Go to Store <ArrowUpRight className="w-4 h-4" />
                  </a>
                </div>
              </div>
            );
          })}
        </div>
      </div>

      {/* Historical Price Trend Graph */}
      {priceHistory.length > 0 && (
        <div className="bg-white p-6 rounded-xl border border-gray-200 shadow-sm space-y-4">
          <h2 className="text-lg font-bold text-gray-900">30-Day Price Trend History</h2>
          <div className="h-64 w-full">
            <ResponsiveContainer width="100%" height="100%">
              <LineChart data={priceHistory}>
                <XAxis
                  dataKey="recorded_at"
                  tickFormatter={(str) => new Date(str).toLocaleDateString('en-US', { month: 'numeric', day: 'numeric' })}
                  stroke="#9CA3AF"
                  fontSize={12}
                />
                <YAxis domain={['dataMin - 10', 'dataMax + 10']} stroke="#9CA3AF" fontSize={12} />
                <Tooltip
                  labelFormatter={(str) => new Date(str).toLocaleString()}
                  formatter={(val: number) => [`$${val.toFixed(2)}`, 'Price']}
                />
                <Legend />
                <Line
                  type="monotone"
                  dataKey="price"
                  name="Price ($)"
                  stroke="#10B981"
                  strokeWidth={2}
                  dot={false}
                />
              </LineChart>
            </ResponsiveContainer>
          </div>
        </div>
      )}

    </div>
  );
}