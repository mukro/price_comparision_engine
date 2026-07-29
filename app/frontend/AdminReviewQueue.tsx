'use client';

import React, { useEffect, useState } from 'react';
import { Check, X, ExternalLink } from 'lucide-react';

interface PendingMatch {
  offer_id: string;
  vendor_title: string;
  current_price: number;
  vendor_name: string;
  suggested_product_id: string;
  suggested_product_title: string;
}

export default function AdminReviewQueue() {
  const [items, setItems] = useState<PendingMatch[]>([]);
  const API_BASE = process.env.NEXT_PUBLIC_API_URL || 'http://localhost:8000/api/v1';

  useEffect(() => {
    fetchPending();
  }, []);

  async function fetchPending() {
    const res = await fetch(`${API_BASE}/admin/pending-matches`);
    if (res.ok) setItems(await res.json());
  }

  async function handleDecision(offer_id: string, approved: boolean) {
    await fetch(`${API_BASE}/admin/review-match`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ offer_id, approved }),
    });
    // Remove item from UI state immediately
    setItems((prev) => prev.filter((i) => i.offer_id !== offer_id));
  }

  return (
    <div className="max-w-5xl mx-auto p-6 space-y-4 font-sans">
      <h1 className="text-xl font-bold text-gray-900">Entity Matcher Review Queue</h1>
      <p className="text-sm text-gray-500">
        Review medium-confidence product matches before publishing them to the public catalog.
      </p>

      {items.length === 0 ? (
        <div className="p-8 bg-gray-50 border rounded-lg text-center text-gray-500">
          No pending matches requiring review. All catalogs are clean!
        </div>
      ) : (
        <div className="space-y-3">
          {items.map((item) => (
            <div key={item.offer_id} className="p-4 bg-white border rounded-lg flex items-center justify-between gap-4 shadow-sm">
              <div className="flex-1 space-y-1">
                <span className="text-xs font-bold text-indigo-600 uppercase">{item.vendor_name} Listing</span>
                <p className="text-sm font-semibold text-gray-900">{item.vendor_title}</p>
                <span className="text-xs text-gray-500">Scraped Price: ${item.current_price.toFixed(2)}</span>
              </div>

              <div className="text-gray-400 font-bold">VS</div>

              <div className="flex-1 space-y-1">
                <span className="text-xs font-bold text-emerald-600 uppercase">Suggested Master Product</span>
                <p className="text-sm font-semibold text-gray-900">{item.suggested_product_title}</p>
              </div>

              <div className="flex items-center gap-2">
                <button
                  onClick={() => handleDecision(item.offer_id, true)}
                  className="p-2 bg-emerald-50 text-emerald-700 hover:bg-emerald-100 rounded-lg font-medium flex items-center gap-1 text-xs"
                >
                  <Check className="w-4 h-4" /> Approve Match
                </button>
                <button
                  onClick={() => handleDecision(item.offer_id, false)}
                  className="p-2 bg-red-50 text-red-700 hover:bg-red-100 rounded-lg font-medium flex items-center gap-1 text-xs"
                >
                  <X className="w-4 h-4" /> Separate Entry
                </button>
              </div>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}