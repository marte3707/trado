import sys, math, pandas as pd
eq=pd.read_csv(sys.argv[1],parse_dates=["timestamp"]).set_index("timestamp")
def st(s):
    r=s.pct_change().dropna(); y=(s.index[-1]-s.index[0]).days/365.25
    return "CAGR %6.1f%%  Sharpe %5.2f  MaxDD %6.1f%%"%(((s.iloc[-1]/s.iloc[0])**(1/y)-1)*100, r.mean()/r.std()*math.sqrt(365), (s/s.cummax()-1).min()*100)
for a,b in [("2016","2026"),("2016","2020"),("2021","2026"),("2022","2022"),("2024","2026"),("2025","2026")]:
    s=eq.loc[a:b]
    print(f"{a}-{b}  strat {st(s['equity'])} | B&H {st(s['benchmark_equity'])}")
