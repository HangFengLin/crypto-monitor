"""Run the credential-free collector with an immutable activation receipt."""
import argparse
import asyncio
import json
import sys
from .config import Activation,implementation_sha
from .storage import Store
from .collector import CollectorCore,PublicRuntime


def main():
    parser=argparse.ArgumentParser(description='金丹v1.0 public observation collector; no orders/accounts')
    parser.add_argument('--state-dir',required=True)
    parser.add_argument('--activation',required=True)
    parser.add_argument('--max-state-bytes',type=int,default=4*1024**3)
    parser.add_argument('--min-free-bytes',type=int,default=2*1024**3)
    args=parser.parse_args()
    store=None
    try:
        activation=Activation.load(args.activation)
        if args.max_state_bytes<=0 or args.min_free_bytes<0:raise ValueError('Storage limits must be finite positive bounds')
        store=Store(args.state_dir,args.max_state_bytes,args.min_free_bytes)
        core=CollectorCore(store,activation)
        print(json.dumps(dict(event='collector_start',version='1.0.0',code_sha256=implementation_sha(),
                              activation_sha256=activation.document_sha,mode='observation_only',orders_enabled=False)),flush=True)
        asyncio.run(PublicRuntime(core).run())
    except KeyboardInterrupt:return 0
    except Exception as error:
        print(json.dumps(dict(event='collector_start_failed',error_type=type(error).__name__,orders_enabled=False)),file=sys.stderr,flush=True)
        return 1
    finally:
        if store is not None:store.close()
    return 0


if __name__=='__main__':raise SystemExit(main())
